"""Plain-language command failures and their exit codes."""

EXIT_FAILURE = 1
EXIT_AUTHENTICATION = 3
EXIT_DENIED = 4
EXIT_NOT_FOUND = 5
EXIT_CONFLICT = 6
EXIT_RATE_LIMITED = 7
EXIT_UNAVAILABLE = 8
EXIT_REJECTED = 9
EXIT_RUNNING = 10

# One fixed sentence per HTTP status, used when the server's own text adds nothing.
STATUS_SENTENCES = {
    400: 'Faxbot did not accept these values.',
    401: 'Faxbot did not accept the API key. Check --key, FAXBOT_API_KEY or your saved profile.',
    403: 'This API key is not allowed to do that.',
    404: 'Faxbot could not find that, or this API key cannot see it.',
    409: 'That conflicts with the current state of Faxbot. Check it and try again.',
    413: 'The file is too large for this Faxbot installation.',
    415: 'Faxbot cannot fax this type of file. Send a PDF or a plain text file.',
    422: 'Faxbot did not accept these values.',
    429: 'Too many requests. Wait a moment and try again.',
}

STATUS_EXIT_CODES = {401: EXIT_AUTHENTICATION, 403: EXIT_DENIED, 404: EXIT_NOT_FOUND, 409: EXIT_CONFLICT,
                     429: EXIT_RATE_LIMITED, 400: EXIT_REJECTED, 413: EXIT_REJECTED, 415: EXIT_REJECTED,
                     422: EXIT_REJECTED}

# Server texts that say less than the fixed sentence.
_GENERIC = {
    'Not Found', 'Access target not found.', 'This operation is not permitted.', 'Invalid access request.',
    'Authentication required or credentials no longer valid.', 'Too many authentication attempts. Try again later.',
    'Credential transport or browser origin is not allowed.', 'Method Not Allowed', 'Invalid credential input.',
}
# Server texts written for the console, reworded for the command line.
_TRANSLATED = {
    'Access policy changed. Reload and try again.':
        'Access settings changed while this command was running. Run it again.',
    'Configuration changed; reload before applying edits.':
        'Settings changed while this command was running. Run it again.',
    'Browser request verification failed. Refresh your session and try again.':
        'Faxbot did not accept this request. Sign in again and retry.',
    'Inbound not enabled': 'Receiving faxes is turned off on this installation.',
    'v3 plugins feature disabled': 'Provider plugins are turned off on this installation.',
}


class CliError(Exception):
    """A failure the user can act on, reported as one plain sentence."""

    def __init__(self, message, exit_code=EXIT_FAILURE, *, status=None, detail=None):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.status = status
        self.detail = detail

    def as_dict(self):
        result = {'message': self.message, 'exit_code': self.exit_code}
        if self.status is not None:
            result['status'] = self.status
            result['detail'] = self.detail
        return result


def _field_names(detail):
    names = []
    for item in detail:
        location = item.get('loc') if isinstance(item, dict) else None
        if isinstance(location, list) and location:
            name = str(location[-1])
            if name not in names and name != 'body':
                names.append(name)
    return names


def http_error(status, detail):
    """Map an API error response to one plain sentence and an exit code.

    Fax, case, routing and intake routes already explain a 400, 404 or 409 in
    plain words, so a specific server sentence is shown instead of the fixed one.
    """
    exit_code = STATUS_EXIT_CODES.get(status, EXIT_UNAVAILABLE if status >= 500 else EXIT_FAILURE)
    message = None
    if isinstance(detail, str) and status not in (401, 429):
        text = detail.strip()
        if text in _TRANSLATED:
            message = _TRANSLATED[text]
        elif text and text not in _GENERIC and len(text) <= 300:
            message = text
    elif isinstance(detail, list) and status in (400, 422):
        names = _field_names(detail)
        if names:
            message = 'Faxbot did not accept these values: ' + ', '.join(names) + '.'
    if message is None:
        if status in STATUS_SENTENCES:
            message = STATUS_SENTENCES[status]
        elif status >= 500:
            message = 'Faxbot could not complete this right now. Try again shortly.'
        else:
            message = f'Faxbot refused this request (HTTP {status}).'
    return CliError(message, exit_code, status=status, detail=detail)
