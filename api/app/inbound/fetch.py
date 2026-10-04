"""Bounded provider requests for received faxes.

Faxbot only asks a provider's documented API host for a received fax, using
the fax ID and the configured account; it never requests an address that a
notification supplies. Requests time out after 30 seconds, never follow a
redirect, and stop reading a document after 50 MB.
"""
import json
from urllib.parse import urlsplit

import httpx


MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 30.0
PHAXIO_HOSTS = frozenset({'api.phaxio.com'})
SINCH_HOSTS = frozenset({'fax.api.sinch.com', 'us.fax.api.sinch.com', 'eu.fax.api.sinch.com'})

# Tests replace this with an ``httpx.MockTransport``; production uses the network.
_TRANSPORT = None


class FetchError(RuntimeError):
    """A plain sentence a person can act on; never includes credentials or URLs."""


def require_host(url, hosts, provider):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FetchError(f'The {provider} address in settings is not a {provider} API address.') from None
    if (parsed.scheme != 'https' or parsed.hostname not in hosts or parsed.username or parsed.password
            or port not in (None, 443)):
        raise FetchError(f'The {provider} address in settings is not a {provider} API address.')
    return url


def _client():
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=_TRANSPORT)


async def _read(response, limit, provider, what):
    declared = response.headers.get('content-length')
    if declared and declared.isdigit() and int(declared) > limit:
        raise FetchError(f'{provider} sent {what} larger than Faxbot accepts.')
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise FetchError(f'{provider} sent {what} larger than Faxbot accepts.')
    return bytes(body)


async def get_json(url, *, auth, hosts, provider):
    """Return ``(status_code, payload)``; payload is None unless the body is a JSON object."""
    require_host(url, hosts, provider)
    try:
        async with _client() as client:
            async with client.stream('GET', url, auth=auth, headers={'Accept': 'application/json'}) as response:
                body = await _read(response, MAX_METADATA_BYTES, provider, 'an answer')
                status = response.status_code
    except FetchError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL):
        raise FetchError(f'Faxbot could not reach {provider}.') from None
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    return status, payload if isinstance(payload, dict) else None


async def get_document(url, *, auth, hosts, provider, max_bytes=MAX_DOCUMENT_BYTES):
    """Return the document bytes of a 200 answer; anything else is a plain FetchError."""
    require_host(url, hosts, provider)
    try:
        async with _client() as client:
            async with client.stream('GET', url, auth=auth, headers={'Accept': 'application/pdf'}) as response:
                if response.status_code in (401, 403):
                    raise FetchError(f'{provider} refused the configured API key.')
                if response.status_code == 404:
                    raise FetchError(f'{provider} does not have the document for this fax yet.')
                if response.status_code != 200:
                    raise FetchError(f'{provider} did not send the document (HTTP {response.status_code}).')
                return await _read(response, max_bytes, provider, 'a document')
    except FetchError:
        raise
    except (httpx.HTTPError, httpx.InvalidURL):
        raise FetchError(f'Faxbot could not reach {provider}.') from None
