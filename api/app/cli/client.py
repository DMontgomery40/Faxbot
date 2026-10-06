"""HTTP client for the Faxbot API. One request per call: nothing is retried automatically."""
from urllib.parse import quote

import httpx

from .errors import EXIT_AUTHENTICATION, EXIT_UNAVAILABLE, CliError, http_error

USER_AGENT = 'faxbot-cli/1'


def default_client_factory(url, timeout):
    """A new HTTP client; the second value says the caller should close it."""
    return httpx.Client(base_url=url, timeout=timeout, follow_redirects=False), True


def segment(value):
    """One path segment, with every reserved character escaped."""
    return quote(str(value), safe='')


class Api:
    def __init__(self, url, key, *, client_factory=None, timeout=60.0):
        self.url = url.rstrip('/')
        self._key = key
        self._factory = client_factory or default_client_factory
        self._timeout = timeout
        self._client = None
        self._owned = False

    def _http(self):
        if self._client is None:
            self._client, self._owned = self._factory(self.url, self._timeout)
        return self._client

    def close(self):
        if self._client is not None and self._owned:
            self._client.close()
        self._client = None

    def request(self, method, path, *, auth=True, params=None, json=None, data=None, files=None,
                headers=None, raw=False, allow=()):
        """Send one request; return parsed JSON (or the response when raw) or raise CliError.

        Statuses in allow are answers, not failures (for example 503 from readiness).
        """
        sent = {'User-Agent': USER_AGENT, 'Accept': 'application/json'}
        if auth:
            if not self._key:
                raise CliError("No API key. Use --key, set FAXBOT_API_KEY, or save one with "
                               "'faxbot system profiles save'.", EXIT_AUTHENTICATION)
            sent['X-API-Key'] = self._key
        sent.update(headers or {})
        if params:
            params = {name: value for name, value in params.items() if value is not None}
        try:
            response = self._http().request(method, path, params=params or None, json=json, data=data,
                                            files=files, headers=sent)
        except httpx.TimeoutException:
            raise CliError(f'Faxbot at {self.url} did not answer in time. Nothing was retried; check before '
                           'running the command again.', EXIT_UNAVAILABLE) from None
        except httpx.HTTPError:
            raise CliError(f'Could not reach Faxbot at {self.url}. Check that it is running and that the '
                           'address is right.', EXIT_UNAVAILABLE) from None
        if response.status_code >= 400 and response.status_code not in allow:
            try:
                detail = response.json().get('detail')
            except (ValueError, AttributeError):
                detail = None
            raise http_error(response.status_code, detail)
        if raw:
            return response
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise CliError('Faxbot sent an answer this command does not understand. Check the server address.',
                           EXIT_UNAVAILABLE) from None

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def put(self, path, **kwargs):
        return self.request('PUT', path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request('PATCH', path, **kwargs)

    def delete(self, path, **kwargs):
        return self.request('DELETE', path, **kwargs)

    def policy_version(self):
        """The current access policy version, read immediately before a change."""
        return self.get('/auth/me')['policy_version']

    def with_policy(self, body):
        """The body plus expected_policy_version, fetched from /auth/me just now."""
        return {**body, 'expected_policy_version': self.policy_version()}

    def pages(self, path, *, params=None, limit=None, page_size=200):
        """Every item of a cursor-paginated /access list, up to limit items."""
        params = dict(params or {})
        cursor = None
        items = []
        while True:
            wanted = page_size if limit is None else max(1, min(page_size, limit - len(items)))
            page = self.get(path, params={**params, 'cursor': cursor, 'limit': wanted})
            items.extend(page.get('items', []))
            cursor = page.get('next_cursor')
            if cursor is None or (limit is not None and len(items) >= limit):
                return items if limit is None else items[:limit]
