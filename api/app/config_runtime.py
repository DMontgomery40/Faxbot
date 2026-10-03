"""Installation startup ownership and immutable request configuration frames."""
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
import asyncio

import anyio
import sqlalchemy as sa
from starlette.responses import JSONResponse

from .config import (bootstrap_locations, install_configuration_source,
                     release_configuration_source, use_configuration)
from .config_activation import ConfigurationManager, ConfigurationActivationError, compile_profiles
from .config_bootstrap import load_bootstrap_configuration, ConfigurationBootstrapError
from .config_lifecycle import InstallationLifecycle
from .config_store import ConfigurationStore, ConfigurationNotInitialized


async def run_lifecycle_step(operation):
    """A cancelled startup must join its file/DB work before releasing ownership."""
    # Shutdown cancels child Tasks too; hold the executor Future directly so
    # cancellation cannot masquerade as completion of the ownership thread.
    task = asyncio.get_running_loop().run_in_executor(None, copy_context().run, operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Shutdown may cancel more than once. Never forward those cancellations
        # to the wrapper task: its thread still owns lock/database work.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # consume a preparation failure; preserve cancellation
        raise


class ConfigurationRuntime:
    def __init__(self, environment):
        self.environment = dict(environment)
        self.locations = bootstrap_locations(environment)
        self.lifecycle = InstallationLifecycle(Path(self.locations.fax_data_dir))
        self.manager = None
        self.snapshot = None
        self.candidate = None
        self.serving = False

    def prepare(self):
        """Called in the actual worker, before preparing any long-lived resources."""
        from . import db
        directory = Path(self.locations.fax_data_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lifecycle.acquire()
        try:
            self._require_stopped_schema_upgrade()
            with use_configuration(self.locations):
                db.init_db()
            key_path = self.environment.get('FAXBOT_INSTALLATION_KEY_PATH') or str(directory / '.configuration.key')
            store = ConfigurationStore(db.engine, key_path)
            self.manager = ConfigurationManager(store)
            try:
                snapshot = store.read()
            except ConfigurationNotInitialized:
                imported = load_bootstrap_configuration(self.environment)
                # The deployment must locate its store and lock/key directory
                # before reading that store. A legacy file cannot redirect them.
                if (imported.values.database_url != self.locations.database_url
                        or Path(imported.values.fax_data_dir).absolute() != directory.absolute()):
                    raise ConfigurationBootstrapError(
                        'Set deployment DATABASE_URL and FAX_DATA_DIR to the legacy persisted locations before starting; no configuration was imported.')
                state = imported.plugins.as_dict()
                catalog = self.manager.catalog_loader(imported.values)
                snapshot = store.initialize(imported.values, actor='bootstrap', plugins=state,
                    providers=compile_profiles(imported.values, catalog, state))
            if (snapshot.active.values.database_url != self.locations.database_url
                    or Path(snapshot.active.values.fax_data_dir).absolute() != directory.absolute()):
                raise ConfigurationBootstrapError('Deployment storage locations do not match this installation; use the maintenance transfer workflow.')
            self.snapshot = snapshot
            self.candidate = snapshot.desired if self.lifecycle.can_promote else snapshot.active
            self._check_telephony_drain()
            return self
        except BaseException:
            self.lifecycle.close()
            raise

    def _require_stopped_schema_upgrade(self):
        """Mixed old/new delivery writers cannot coexist during a migration."""
        if self.lifecycle.can_promote:
            return
        from .schema import HEAD, create_database_engine
        engine = create_database_engine(self.locations.database_url)
        try:
            with engine.connect() as connection:
                revisions = (connection.execute(sa.text('SELECT version_num FROM alembic_version')).scalars().all()
                             if sa.inspect(connection).has_table('alembic_version') else [])
            if revisions != [HEAD]:
                raise ConfigurationBootstrapError(
                    'Stop all Faxbot workers before upgrading the installation database; no migration was attempted.')
        except sa.exc.SQLAlchemyError:
            raise ConfigurationBootstrapError('Cannot verify installation schema before worker startup.') from None
        finally:
            engine.dispose()

    def _check_telephony_drain(self):
        if self.candidate is not self.snapshot.pending:
            return
        store = self.manager.store
        old_ami = any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                      for _, identity in self.snapshot.active.profiles)
        new_ami = any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                      for _, identity in self.candidate.profiles)
        fields = ('ami_host', 'ami_port', 'ami_username', 'ami_password')
        replaces_ami = old_ami and (not new_ami or self.candidate.values.fax_disabled or any(
            getattr(self.snapshot.active.values, field) != getattr(self.candidate.values, field) for field in fields))
        if not replaces_ami:
            return
        from .outbound_store import TERMINAL
        deliveries = store.delivery_tables['outbound_deliveries']
        resolved = tuple(TERMINAL | {'held'})
        with store.engine.connect() as connection:
            # Includes legacy SIP jobs with no provable binding. Do not silently
            # disconnect their old result channel or infer a replacement account.
            unresolved = connection.execute(sa.select(store.jobs.c.id).join(
                deliveries, deliveries.c.id == store.jobs.c.id).where(
                store.jobs.c.backend == 'sip', deliveries.c.state.not_in(resolved)).limit(1)).first()
            identities = connection.execute(sa.select(store.job_bindings.c.profile_id).join(
                store.jobs, store.jobs.c.id == store.job_bindings.c.id).where(
                store.jobs.c.id.in_(sa.select(deliveries.c.id).where(
                    deliveries.c.state.not_in(resolved)))).distinct()).scalars().all()
        captured_ami = any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                           for identity in identities)
        if unresolved is not None or captured_ami:
            raise ConfigurationActivationError(
                'Unresolved AMI faxes must be drained or reconciled before replacing the AMI connection; pending settings were retained.')

    @contextmanager
    def frame(self, revision=None):
        revision = revision or self.manager.store.read().active
        profiles = {role: self.manager.store.read_profile(identity) for role, identity in revision.profiles}
        with use_configuration(revision.values, profiles):
            yield revision

    def publish_ready(self):
        """Only after the candidate's resources have initialized successfully."""
        if self.candidate is self.snapshot.pending:
            self.snapshot = self.manager.store.promote_pending(self.snapshot, lifecycle=self.lifecycle)
        install_configuration_source(self.manager.store)
        try:
            self.lifecycle.mark_serving()
        except BaseException:
            release_configuration_source(self.manager.store)
            raise
        self.serving = True

    def close(self):
        if self.manager is not None:
            release_configuration_source(self.manager.store)
        self.serving = False
        self.lifecycle.close()


class ConfigurationMiddleware:
    """Pin one active revision across the complete HTTP/WebSocket operation."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            await self.app(scope, receive, send)
            return
        runtime = getattr(scope['app'].state, 'configuration_runtime', None)
        if runtime is None or not runtime.serving:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await JSONResponse({'detail': 'Installation configuration is not ready.'}, status_code=503)(scope, receive, send)
            return
        try:
            snapshot = await anyio.to_thread.run_sync(runtime.manager.store.read)
            profiles = await anyio.to_thread.run_sync(lambda: {
                role: runtime.manager.store.read_profile(identity) for role, identity in snapshot.active.profiles})
        except Exception:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await JSONResponse({'detail': 'Installation configuration is unavailable.'}, status_code=503)(scope, receive, send)
            return
        scope['faxbot.configuration'] = snapshot
        with use_configuration(snapshot.active.values, profiles):
            await self.app(scope, receive, send)
