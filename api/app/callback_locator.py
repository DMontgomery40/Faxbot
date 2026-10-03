"""Build the exact public callback URL submitted by a captured attempt."""
from urllib.parse import unquote_plus, urlencode, urlsplit, urlunsplit


def callback_base_url(revision, profile):
    """Use the captured override or this installation's captured public URL."""
    configuration = profile.configuration
    explicit = configuration.settings.get('callback_url')
    if explicit:
        return explicit
    return revision.values.public_api_url.rstrip('/') + '/' + configuration.provider_id + '-callback'


def callback_url_with_locators(callback_url: str, job_id: str, attempt_id: str | None = None) -> str:
    """Preserve unrelated query bytes/order; replace reserved locators once.

    The caller supplies its captured public URL, never proxy/request headers.
    Omitting attempt_id removes stale attempt locators for the legacy interface.
    """
    try:
        if (not isinstance(callback_url, str) or not callback_url
                or any(ord(char) <= 32 or ord(char) == 127 for char in callback_url)
                or '#' in callback_url):
            raise ValueError
        parsed = urlsplit(callback_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError
        parsed.port
        if (not isinstance(job_id, str) or not job_id
                or (attempt_id is not None and (not isinstance(attempt_id, str) or not attempt_id))):
            raise ValueError
        callback_url.encode('utf-8')
        remaining = [part for part in parsed.query.split('&') if
            unquote_plus(part.partition('=')[0]) not in {'job_id', 'attempt_id'}] if parsed.query else []
        locators = [('job_id', job_id)]
        if attempt_id is not None:
            locators.append(('attempt_id', attempt_id))
        query = '&'.join(remaining + [urlencode(locators)])
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ''))
    except (TypeError, ValueError):
        raise ValueError('Invalid public callback URL or locator.') from None
