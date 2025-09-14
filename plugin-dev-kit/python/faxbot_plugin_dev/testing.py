from typing import Dict, Any
from unittest.mock import MagicMock
import tempfile
import shutil
import asyncio

from .base import PluginDeps, PluginManifest


def create_mock_deps() -> PluginDeps:
    logger = MagicMock()
    storage = MagicMock()
    db = MagicMock()
    events = MagicMock()
    http = MagicMock()
    audit = MagicMock()
    cache = MagicMock()
    metrics = MagicMock()

    events._events = {}

    def emit(event: str, payload: Dict[str, Any] = None):
        payload = payload or {}
        events._events.setdefault(event, []).append(payload)

    def get_events(event: str):
        return events._events.get(event, [])

    events.emit = emit
    events.get_events = get_events

    config_dir = tempfile.mkdtemp(prefix="faxbot_plugin_")

    return PluginDeps(
        logger=logger,
        storage=storage,
        db=db,
        events=events,
        http=http,
        audit=audit,
        config_dir=config_dir,
        cache=cache,
        metrics=metrics,
        extras={},
    )


class PluginTestCase:
    def __init__(self, plugin_class):
        self.plugin_class = plugin_class
        self.plugin = None
        self.deps = None

    async def setup(self, config: Dict[str, Any] = None):
        self.deps = create_mock_deps()
        self.plugin = self.plugin_class(self.deps)
        if config:
            self.plugin.validate_config(config)
            await maybe_await(self.plugin.initialize(config))
        return self

    async def teardown(self):
        if self.plugin:
            await maybe_await(self.plugin.shutdown())
        if self.deps and getattr(self.deps, 'config_dir', None):
            shutil.rmtree(self.deps.config_dir, ignore_errors=True)

    def assert_event_emitted(self, event: str):
        events = self.deps.events.get_events(event)
        assert events, f"Event {event} was not emitted"

    def assert_no_phi_logged(self):
        for call in self.deps.logger.mock_calls:
            args = str(call)
            import re
            if re.search(r'\d{10,}', args):
                assert "***" in args, "PHI (phone number) detected in logs"


async def maybe_await(result):
    if asyncio.iscoroutine(result):
        return await result
    return result

