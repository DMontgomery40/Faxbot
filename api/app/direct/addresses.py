"""Partner endpoint addresses: direct delivery only talks to public Internet hosts.

A partner card names an endpoint URL. Before enrolling a partner and before every
request to it, the host name is resolved and every address it resolves to must be a
public one: loopback, link-local, private (RFC 1918 and IPv6 unique-local),
multicast, unspecified and other reserved addresses are refused. Requests then go to
the address that was checked, so a later DNS answer cannot redirect them.

DIRECT_ALLOW_PRIVATE_PEERS=true turns the check off, for partners on a network the
operator controls and for local testing.
"""
import ipaddress
import socket
from urllib.parse import urlsplit, urlunsplit


PRIVATE_REFUSED = ("The partner's address points to a private or local network, which direct delivery refuses "
                   'unless DIRECT_ALLOW_PRIVATE_PEERS is turned on.')
NOT_FOUND = "Faxbot could not find the partner's address; check the card and try again."


class PartnerAddressError(ValueError):
    """The endpoint is not a public Internet address; the message is one sentence for people."""


def public(address):
    """Whether an IP address is a public Internet address a partner may use."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_unspecified


def resolve(host, port):
    """Every address the host name resolves to."""
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def checked_address(url, *, resolver=resolve):
    """The public address to connect to for url, or PartnerAddressError."""
    parts = urlsplit(url)
    if not parts.hostname:
        raise PartnerAddressError(NOT_FOUND)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    try:
        addresses = resolver(parts.hostname, port)
    except (OSError, UnicodeError):
        raise PartnerAddressError(NOT_FOUND) from None
    if not addresses:
        raise PartnerAddressError(NOT_FOUND)
    try:
        all_public = all(public(address) for address in addresses)
    except ValueError:
        raise PartnerAddressError(NOT_FOUND) from None
    if not all_public:
        raise PartnerAddressError(PRIVATE_REFUSED)
    return addresses[0]


def pinned_request(url, address, kwargs):
    """The URL and request options that reach the checked address with the original host name.

    The Host header keeps the partner's name, and for HTTPS the TLS server name
    (and certificate check) stays the partner's host name.
    """
    parts = urlsplit(url)
    host = f'[{address}]' if ':' in address else address
    netloc = host + (f':{parts.port}' if parts.port else '')
    options = dict(kwargs)
    options['headers'] = {**(kwargs.get('headers') or {}), 'Host': parts.netloc.rpartition('@')[2]}
    if parts.scheme == 'https':
        options['extensions'] = {**(kwargs.get('extensions') or {}), 'sni_hostname': parts.hostname}
    return urlunsplit(parts._replace(netloc=netloc)), options
