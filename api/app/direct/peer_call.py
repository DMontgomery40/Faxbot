"""Peer fax call (M1b): a fax call straight between two Faxbots' Asterisks, never on the telephone network.

Some partners need a real fax session, for example when their own fax server
is their intake. Then a SIP call can go directly between the two
installations' Asterisks, with IAF (Internet-aware fax: no pacing for the
telephone network). It costs nothing per minute and needs no carrier.

T.38 (UDPTL) and audio (RTP) carry the pages unencrypted, so a peer fax call is
allowed only inside an encrypted tunnel, such as WireGuard, to an address that
is not on the public internet. ``decide`` is the only gate: the PJSIP peer
endpoint that will place these calls (``sip_trunk.py``, a follow-up) must call
``require_tunnel`` and render nothing when it refuses. Without a tunnel a peer
fax call is impossible.

When it applies (all of these, checked in this order):

1. the partner is enrolled, verified and unexpired;
2. the partner does not accept fax images from us: a fax image (M1a) needs no
   call at all, so it always goes first;
3. the partner said, in a statement it signed, that it takes peer fax calls;
4. we have the partner's call address inside the tunnel (an IP address, never
   a host name, so a later DNS answer cannot move it);
5. that address is not a public internet address;
6. the route to that address leaves through an encrypted tunnel interface on
   this machine (WireGuard: the interface's kernel type is ``wireguard``).

The route check reads Linux's routing table (``ip -o route get``) and the
interface type (``/sys/class/net/<name>/uevent``). It must run in the network
namespace Asterisk sends from: before each peer fax call Faxbot asks Asterisk
itself (``ami.peer_route``: a Local channel runs ``asterisk/bin/faxbot-peer-route``
in the Asterisk container and reports the interface and its kind; nothing is
dialed). The tunnel is up only when that route leaves through WireGuard and the
partner's Asterisk answers Asterisk's checks over it (its contact is reachable).

The endpoint (``render_peers``, written into Asterisk's ``pjsip.conf`` after the
trunks by ``sip_trunk.write_asterisk_configuration``): one per verified partner
with a private tunnel address that takes our calls or whose calls we take, on a
transport of its own (UDP port ``PEER_PORT`` on every address, with no public
address rewriting: the tunnel's addresses are private on both sides). Its calls
carry ``FAXBOT_PEER`` (the partner's enrollment ID) and ``FAXBOT_IAF=peer``, and
an inbound call from the partner's tunnel address is the partner's (identify by
address). The WireGuard tunnel itself is set up outside Faxbot, inside the
Asterisk container's network (a WireGuard container sharing it).
"""
from dataclasses import dataclass
from datetime import datetime
import ipaddress
import logging
from pathlib import Path
import re
import subprocess

from .addresses import public


TUNNEL_KINDS = frozenset({'wireguard'})
# The peer fax calls' own SIP port on every address (transport-peer), so they never share the carrier's transport
# and its public-address rewriting; a partner's address without a port means this one.
PEER_PORT = 5070
PEER_TRANSPORT = 'transport-peer'
_PEER_ID = re.compile(r'[a-f0-9]{32}')
PEER_ENDPOINT = re.compile(r'peer-[a-f0-9]{32}-endpoint')
TUNNEL_NOTE = ('Set up the WireGuard tunnel to this partner yourself, inside the fax engine\'s network (a WireGuard '
               'container that shares the Asterisk container\'s network). Faxbot only places calls through it.')


class PeerCallRefused(RuntimeError):
    """A peer fax call must not be placed; the message is one sentence for people."""


@dataclass(frozen=True)
class Tunnel:
    interface: str
    kind: str


@dataclass(frozen=True)
class PeerCallDecision:
    applies: bool
    reason: str
    sentence: str
    address: str | None = None
    port: int | None = None
    tunnel: Tunnel | None = None


def parse_address(text):
    """(IP address, port) from "10.20.0.2", "10.20.0.2:5070" or "[fd00::2]:5070"; None when it is not one. Without a
    port, the peer calls' own port (``PEER_PORT``)."""
    text = (text or '').strip()
    match = re.fullmatch(r'\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?', text) or re.fullmatch(
        r'([0-9.]+)(?::(\d{1,5}))?', text) or re.fullmatch(r'([0-9A-Fa-f:]+)()', text)
    if match is None:
        return None
    try:
        address = str(ipaddress.ip_address(match.group(1)))
    except ValueError:
        return None
    port = int(match.group(2)) if match.group(2) else PEER_PORT
    return (address, port) if 0 < port < 65536 else None


def tunnel_address(text):
    """A partner's address inside the tunnel as Faxbot stores it ("10.20.0.2:5070", "[fd00::2]:5070"); raises
    DirectConflict with one sentence when it is no IP address or is on the public internet."""
    from .store import DirectConflict
    parsed = parse_address(text)
    if parsed is None:
        raise DirectConflict("Enter the partner's address inside the tunnel, such as 10.20.0.2 or 10.20.0.2:5070.")
    address, port = parsed
    if public(address):
        raise DirectConflict('That address is on the public internet; a fax call to another Faxbot runs only inside '
                             'an encrypted tunnel, at a private address.')
    return f'[{address}]:{port}' if ':' in address else f'{address}:{port}'


def peer_calls_text(peer):
    """One sentence on fax calls inside the tunnel with this partner, or None when there is nothing to say."""
    if not peer or peer.get('state') == 'revoked':
        return None
    takes, ours = _flag(peer.get('partner_peer_calls')), _flag(peer.get('receive_peer_calls'))
    address = peer.get('peer_call_address')
    if takes and address:
        return (f'Their Faxbot takes fax calls inside your encrypted tunnel at {address}: faxes to them go that way '
                'when the tunnel is up, with no carrier.')
    if takes:
        return ("Their Faxbot takes fax calls inside an encrypted tunnel. Enter its address inside the tunnel to "
                'use it.')
    if ours and address:
        return f'You take their fax calls inside the tunnel from {address}.'
    if ours:
        return "You take their fax calls inside the tunnel once you enter their address inside it."
    return None


def route_interface(address, *, run=subprocess.run):
    """The interface Linux would send to ``address`` through, or None when it cannot say."""
    try:
        result = run(['ip', '-o', 'route', 'get', address], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'\bdev (\S+)', result.stdout or '') if result.returncode == 0 else None
    return found.group(1) if found else None


def interface_kind(name, *, root=Path('/sys/class/net')):
    """The kernel's type for an interface (``wireguard`` for WireGuard), or None."""
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name or ''):
        return None
    try:
        text = (Path(root) / name / 'uevent').read_text(encoding='ascii', errors='replace')
    except OSError:
        return None
    found = re.search(r'^DEVTYPE=(\S+)$', text, re.MULTILINE)
    return found.group(1) if found else None


def tunnel_for(address, *, lookup=route_interface, kind_of=interface_kind):
    """The encrypted tunnel the route to ``address`` leaves through, or None."""
    name = lookup(address)
    kind = kind_of(name) if name else None
    return Tunnel(name, kind) if kind in TUNNEL_KINDS else None


def _flag(value):
    return value is not None and int(value) == 1


def decide(peer, *, tunnel_lookup=tunnel_for, now=None):
    """Whether a fax to ``peer`` (a ``direct_peers`` row) may go by a peer fax call, and why, in one sentence."""
    name = (peer or {}).get('organization') or 'this partner'
    moment = now or datetime.utcnow()
    if not peer or peer.get('state') != 'verified' or (peer.get('expires_at') is not None and peer['expires_at'] <= moment):
        return PeerCallDecision(False, 'not_verified', f'{name} is not a verified partner.')
    if _flag(peer.get('partner_receives_fax_images')):
        return PeerCallDecision(False, 'fax_image_first',
                                f'Faxes to {name} go as the exact fax image, which needs no call.')
    if not _flag(peer.get('partner_peer_calls')):
        return PeerCallDecision(False, 'partner_cannot', f'{name} has not said it takes fax calls from other Faxbots.')
    parsed = parse_address(peer.get('peer_call_address'))
    if parsed is None:
        return PeerCallDecision(False, 'no_address', f"Faxbot has no address inside the tunnel for {name}'s fax calls.")
    address, port = parsed
    if public(address):
        return PeerCallDecision(False, 'public_address',
                                f"{name}'s call address is on the public internet; a fax call to another Faxbot "
                                'runs only inside an encrypted tunnel.', address, port)
    tunnel = tunnel_lookup(address)
    if tunnel is None:
        return PeerCallDecision(False, 'no_tunnel',
                                f'No encrypted tunnel reaches {name}, so Faxbot will not place a fax call to it.',
                                address, port)
    return PeerCallDecision(True, 'tunnel', f'Faxes to {name} can go by a fax call inside the encrypted tunnel '
                                            f'({tunnel.interface}), never on the telephone network.',
                            address, port, tunnel)


def require_tunnel(peer, **options):
    """The decision when a peer fax call may be placed; PeerCallRefused (with its sentence) otherwise."""
    decision = decide(peer, **options)
    if not decision.applies:
        raise PeerCallRefused(decision.sentence)
    return decision


# -- the endpoint in Asterisk --------------------------------------------------------------------------------

def endpoint_name(peer_id):
    """``peer-<enrollment id>-endpoint``: the PJSIP endpoint of one partner's peer fax calls."""
    if not isinstance(peer_id, str) or not _PEER_ID.fullmatch(peer_id):
        raise ValueError('Unsupported partner')
    return f'peer-{peer_id}-endpoint'


def _expired(peer, moment):
    return peer.get('expires_at') is not None and peer['expires_at'] <= moment


def renderable(peer, *, now=None):
    """(address, port, calls out, calls in) for a partner whose peer fax calls belong in Asterisk's file, or None.

    Verified and unexpired, a private address inside the tunnel, and calls at least one way: the partner said it
    takes our calls (``partner_peer_calls``), or you take its calls (``receive_peer_calls``).
    """
    moment = now or datetime.utcnow()
    if not peer or peer.get('state') != 'verified' or _expired(peer, moment) or not _PEER_ID.fullmatch(peer.get('id') or ''):
        return None
    parsed = parse_address(peer.get('peer_call_address'))
    if parsed is None or public(parsed[0]):
        return None
    out, inbound = _flag(peer.get('partner_peer_calls')), _flag(peer.get('receive_peer_calls'))
    if not (out or inbound):
        return None
    return parsed[0], parsed[1], out, inbound


def render_peers(peers, *, now=None) -> str:
    """The pjsip.conf sections for partners' peer fax calls ('' for none): their own transport, then per partner an
    AOR (checked every 30 seconds over the tunnel), an endpoint and, when you take its calls, an identify by its
    tunnel address. Nothing here reaches a carrier."""
    found = [(peer['id'], *found) for peer in peers or () if (found := renderable(peer, now=now)) is not None]
    if not found:
        return ''
    lines = ['; Peer fax calls with enrolled partners, only inside an encrypted tunnel set up outside Faxbot.',
             '; Faxbot writes these from Recipients > Partners; edits here are replaced.',
             f'[{PEER_TRANSPORT}]', 'type=transport', 'protocol=udp', f'bind=0.0.0.0:{PEER_PORT}', '']
    for peer_id, address, port, out, inbound in sorted(found):
        host = f'[{address}]' if ':' in address else address
        lines += [f'[peer-{peer_id}-aor]', 'type=aor', f'contact=sip:{host}:{port}', 'qualify_frequency=30',
                  'qualify_timeout=3.0', '',
                  f'[peer-{peer_id}-endpoint]', 'type=endpoint', f'transport={PEER_TRANSPORT}',
                  f'aors=peer-{peer_id}-aor', 'context=faxbot-inbound', 'disallow=all', 'allow=ulaw,alaw',
                  't38_udptl=yes', 't38_udptl_ec=redundancy', 't38_udptl_maxdatagram=400', 'direct_media=no',
                  f'set_var=FAXBOT_PEER={peer_id}', 'set_var=FAXBOT_IAF=peer', '']
        if inbound:
            lines += [f'[peer-{peer_id}-identify]', 'type=identify', f'endpoint=peer-{peer_id}-endpoint',
                      f'match={address}', '']
    return '\n'.join(lines)


def installation_peers(engine):
    """Every enrolled partner (``direct_peers`` rows), for ``render_peers``; [] when ``engine`` is None."""
    if engine is None:
        return []
    from .store import DirectStore
    return DirectStore(engine).list_peers()


def loaded(values, peer_id):
    """Whether the running Asterisk loaded this partner's endpoint (Asterisk's file as it started)."""
    from ..sip_trunk import started_configuration_path
    try:
        started = started_configuration_path(values).read_text()
    except OSError:
        return False
    return f'[{endpoint_name(peer_id)}]' in started


# -- before each peer fax call -------------------------------------------------------------------------------

@dataclass(frozen=True)
class PeerCall:
    """A peer fax call Faxbot may place now: the partner, its endpoint and the decision that allowed it."""
    peer_id: str
    endpoint: str
    decision: PeerCallDecision


async def tunnel_in_asterisk(ami, address):
    """The tunnel the route to ``address`` leaves through, as Asterisk's own network sees it, or None."""
    found = await ami.peer_route(address)
    if not found:
        return None
    name, kind = found
    return Tunnel(name, kind) if kind in TUNNEL_KINDS else None


async def reachable(ami, peer_id):
    """Whether the partner's Asterisk answers Asterisk's checks over the tunnel (its contact is Reachable)."""
    response, events = await ami.status_query({'Action': 'PJSIPShowEndpoint', 'Endpoint': endpoint_name(peer_id)},
                                              collect=True)
    if response['response'].lower() != 'success':
        return False
    return any(str(event.get('Status', '')).lower() == 'reachable' for event in events)


async def check(ami, values, peer):
    """Whether a peer fax call to ``peer`` may be placed now, decided inside Asterisk's network; one sentence.

    The gate (``decide``) with the route read in the Asterisk container, then the endpoint loaded and the partner
    answering over the tunnel. Asterisk unreachable or slow: no call (ConnectionError and TimeoutError are the
    manager connection's documented failures).
    """
    name = (peer or {}).get('organization') or 'this partner'
    try:
        parsed = parse_address((peer or {}).get('peer_call_address'))
        tunnel = await tunnel_in_asterisk(ami, parsed[0]) if parsed and not public(parsed[0]) else None
        decision = decide(peer, tunnel_lookup=lambda address: tunnel)
        if not decision.applies:
            return decision
        if not loaded(values, peer['id']):
            return PeerCallDecision(False, 'not_loaded', f"Faxbot's fax engine has not loaded {name}'s fax calls yet; "
                                    'apply your trunk settings to load them.', decision.address, decision.port)
        if not await reachable(ami, peer['id']):
            return PeerCallDecision(False, 'unreachable', f"{name}'s Faxbot does not answer inside the tunnel "
                                    f'({decision.tunnel.interface}), so the fax goes by your carrier.',
                                    decision.address, decision.port, decision.tunnel)
        return decision
    except (ConnectionError, TimeoutError):
        logging.getLogger(__name__).info('Peer fax call check for %s: the fax engine did not answer.', peer.get('id'))
        return PeerCallDecision(False, 'engine', "Faxbot could not ask its fax engine about the tunnel, so the fax "
                                'goes by your carrier.')


async def call_for(ami, values, engine, number):
    """A PeerCall for a fax to ``number`` when it belongs to a verified partner whose peer fax call may go now, else
    None (the fax goes by the carrier as before). Reads only; never raises for a partner that does not qualify."""
    if engine is None or not number:
        return None
    from ..routing.numbers import stored_number
    country = getattr(values, 'fax_default_country', 'US') or 'US'
    wanted = stored_number(number, country=country)
    from ..routing.database import DeliveryStoreError
    try:
        # Read inline, as the rest of the Originate's facts are (ami.originate_fields_for): no await before the
        # check, so concurrent submissions keep their order.
        peers = installation_peers(engine)
    except DeliveryStoreError as error:
        logging.getLogger(__name__).warning('Partners could not be read for a peer fax call (%s); the fax goes by '
                                            'the carrier.', error)
        return None
    for peer in peers:
        if renderable(peer) is None or not _flag(peer.get('partner_peer_calls')):
            continue
        if stored_number(peer.get('phone_number') or '', country=country) != wanted:
            continue
        decision = await check(ami, values, peer)
        if decision.applies:
            return PeerCall(peer['id'], endpoint_name(peer['id']), decision)
        logging.getLogger(__name__).info('Fax to partner %s goes by the carrier: %s', peer['id'], decision.reason)
        return None
    return None
