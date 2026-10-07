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
namespace Asterisk sends from; the follow-up runs it there.
"""
from dataclasses import dataclass
from datetime import datetime
import ipaddress
from pathlib import Path
import re
import subprocess

from .addresses import public


TUNNEL_KINDS = frozenset({'wireguard'})


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
    """(IP address, port) from "10.20.0.2", "10.20.0.2:5060" or "[fd00::2]:5060"; None when it is not one."""
    text = (text or '').strip()
    match = re.fullmatch(r'\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?', text) or re.fullmatch(
        r'([0-9.]+)(?::(\d{1,5}))?', text) or re.fullmatch(r'([0-9A-Fa-f:]+)()', text)
    if match is None:
        return None
    try:
        address = str(ipaddress.ip_address(match.group(1)))
    except ValueError:
        return None
    port = int(match.group(2)) if match.group(2) else 5060
    return (address, port) if 0 < port < 65536 else None


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
