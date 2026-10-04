"""Per-invocation settings: server address, API key, output mode and the HTTP client."""
from contextvars import ContextVar
from dataclasses import dataclass, field

from .client import Api
from .output import Output

_CURRENT = ContextVar('faxbot_cli_state', default=None)


@dataclass
class State:
    url: str
    key: str | None
    profile: str | None
    out: Output
    client_factory: object = None
    admin_options: dict = field(default_factory=dict)
    home_currency: str | None = None
    _api: Api | None = field(default=None, repr=False)

    def api(self):
        if self._api is None:
            self._api = Api(self.url, self.key, client_factory=self.client_factory)
        return self._api

    def close(self):
        if self._api is not None:
            self._api.close()
            self._api = None


def begin(context, value):
    """Make value the State of this invocation; it is closed with the root context."""
    _CURRENT.set(value)
    context.call_on_close(value.close)
    return value


def current():
    value = _CURRENT.get()
    if value is None:
        raise RuntimeError('The faxbot command was not started through its root command.')
    return value


def api():
    return current().api()


def out():
    return current().out
