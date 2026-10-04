"""Installation startup ownership and immutable request configuration frames."""
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
import asyncio
import logging

import anyio
import sqlalchemy as sa
from starlette.responses import JSONResponse

from .config import (bootstrap_locations, install_configuration_source,
                     release_configuration_source, use_configuration)
from .config_activation import ConfigurationManager, ConfigurationActivationError, compile_profiles
from .config_bootstrap import load_bootstrap_configuration, ConfigurationBootstrapError
from .config_lifecycle import InstallationLifecycle
from .config_store import ConfigurationStore, ConfigurationNotInitialized
from .config_values import ConfigurationValues, ConfigurationValueError


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


def _earlier_release_schema(database_url):
    """Whether the database still has a release's tables from before saved configuration."""
    from .schema import create_database_engine
    try:
        engine = create_database_engine(database_url)
    except Exception:
        return False
    try:
        with engine.connect() as connection:
            tables = set(sa.inspect(connection).get_table_names())
        return 'fax_jobs' in tables and 'configuration_state' not in tables
    except sa.exc.SQLAlchemyError:
        return False
    finally:
        engine.dispose()


def _earlier_release_records(engine):
    """Whether the database upgrade found faxes, keys or mailboxes from an earlier release.

    The access migration records what it found in one installation audit entry;
    a new database records zero of everything.
    """
    import json
    found = ('keys_total', 'outbound_personal', 'outbound_legacy', 'inbound_legacy', 'mailbox_resources')
    try:
        with engine.connect() as connection:
            audit = sa.table('access_audit', sa.column('operation'), sa.column('target_kind'), sa.column('details'))
            rows = connection.execute(sa.select(audit.c.details).where(
                audit.c.operation == 'access_migration', audit.c.target_kind == 'installation')).scalars().all()
    except sa.exc.SQLAlchemyError:
        return False
    for details in rows:
        try:
            counts = json.loads(details)
        except (TypeError, ValueError):
            continue
        if isinstance(counts, dict) and any(isinstance(counts.get(name), int) and counts[name] > 0 for name in found):
            return True
    return False


class ConfigurationRuntime:
    def __init__(self, environment):
        self.environment = dict(environment)
        self.locations = bootstrap_locations(environment)
        self.lifecycle = InstallationLifecycle(Path(self.locations.fax_data_dir))
        self.manager = None
        self.snapshot = None
        self.candidate = None
        self.serving = False
        # Settings whose value comes from the environment at every start (names only).
        self.env_managed = frozenset(ConfigurationValues.environment_credentials(self.environment))

    def prepare(self):
        """Called in the actual worker, before preparing any long-lived resources."""
        from . import db
        directory = Path(self.locations.fax_data_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lifecycle.acquire()
        try:
            self._require_stopped_schema_upgrade()
            earlier_schema = _earlier_release_schema(self.locations.database_url)
            with use_configuration(self.locations):
                db.init_db()
            key_path = self.environment.get('FAXBOT_INSTALLATION_KEY_PATH') or str(directory / '.configuration.key')
            store = ConfigurationStore(db.engine, key_path)
            self.manager = ConfigurationManager(store)
            initialized = False
            try:
                snapshot = store.read()
            except ConfigurationNotInitialized:
                initialized = True
                imported = load_bootstrap_configuration(
                    self.environment, earlier_release=earlier_schema or _earlier_release_records(db.engine))
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
            if self.lifecycle.can_promote:
                # Before any other write: a write saves every setting and ends the adoption.
                snapshot = self._adopt_promoted_environment(snapshot)
                snapshot = self._apply_environment_credentials(snapshot)
                snapshot = self._create_engine_password(snapshot, new_installation=initialized)
            self.snapshot = snapshot
            self.candidate = snapshot.desired if self.lifecycle.can_promote else snapshot.active
            self._check_telephony_drain()
            self._share_engine_credentials()
            return self
        except BaseException:
            self.lifecycle.close()
            raise

    def _adopt_promoted_environment(self, snapshot):
        """Settings once read only from the environment keep their variable after an upgrade.

        A saved configuration from before a setting became a configuration value
        takes the variable once, as one revision by "environment". A value that is
        not valid is left out with a warning, so the upgrade still starts.
        """
        supplied = ConfigurationValues.environment_adoptions(self.environment, snapshot.desired.values)
        changes = {}
        for name, (variable, value) in supplied.items():
            try:
                snapshot.desired.values.with_patch({name: value})
            except ConfigurationValueError:
                logging.getLogger(__name__).warning(
                    '%s in the environment is not valid; Faxbot kept its saved setting.', variable)
                continue
            changes[name] = value
        if not changes:
            return snapshot
        return self.manager.apply_environment(snapshot, changes)

    def _apply_environment_credentials(self, snapshot):
        """Credentials in the environment are the values in force: record any that changed.

        Runs at startup before serving, under the installation's startup ownership.
        Unchanged values add no revision; a removed variable leaves the stored value.
        """
        supplied = ConfigurationValues.environment_credentials(self.environment)
        desired = snapshot.desired.values
        changes = {name: value for name, (_, value) in supplied.items() if getattr(desired, name) != value}
        if not changes:
            return snapshot
        try:
            return self.manager.apply_environment(snapshot, changes)
        except ConfigurationValueError:
            variables = ', '.join(sorted(supplied[name][0] for name in changes))
            raise ConfigurationBootstrapError(
                f'A credential set in the environment is not valid ({variables}); fix it in .env, then run docker compose up -d.') from None

    def _uses_engine(self, revision):
        """Whether this revision connects to Faxbot's fax engine (Asterisk) over its manager port."""
        from .config_activation import _routes_need_ami
        store = self.manager.store
        return (any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                    for _, identity in revision.profiles)
                or _routes_need_ami(revision.values, self.manager.catalog_loader(revision.values)))

    def _create_engine_password(self, snapshot, *, new_installation):
        """Create the Asterisk manager password the first time the SIP trunk comes into use.

        The password is plumbing between Faxbot's own two containers, so nobody
        has to type it twice. It is created once, as a setting saved by
        "system", only while the stored password is still the shipped default,
        only when no ASTERISK_AMI_PASSWORD is set in the environment (that
        value always wins), and only when the fax engine connection is new: on
        a new installation, or when the running settings did not use Asterisk
        yet. An installation already connected to an Asterisk of its own keeps
        its password. It takes effect in this same start, before Faxbot
        connects, and is written for Asterisk by _share_engine_credentials.
        """
        import secrets
        desired = snapshot.desired
        if ('ami_password' in self.env_managed or desired.values.ami_password not in ('', 'changeme')
                or not self._uses_engine(desired)):
            return snapshot
        if not new_installation and self._uses_engine(snapshot.active):
            return snapshot
        try:
            result = self.manager.patch(snapshot, {'ami_password': secrets.token_urlsafe(24)}, actor='system')
        except (ConfigurationActivationError, ConfigurationValueError):
            return snapshot
        try:
            from .audit import audit_event
            audit_event('engine_password_created', backend='sip')
        except Exception:
            pass
        return result

    def _share_engine_credentials(self):
        """Write the manager login for the Asterisk container when this start connects to it."""
        from . import sip_trunk
        try:
            if self._uses_engine(self.candidate):
                sip_trunk.write_manager_credentials(self.candidate.values)
        except OSError:
            import logging
            logging.getLogger(__name__).warning('Faxbot could not write the fax engine login for Asterisk.')

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
        from .config_activation import _routes_need_ami
        active, candidate = self.snapshot.active.values, self.candidate.values
        # A SIP extra route uses the same Asterisk connection as a SIP provider.
        old_ami = (any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                       for _, identity in self.snapshot.active.profiles)
                   or _routes_need_ami(active, self.manager.catalog_loader(active)))
        new_ami = (any(store.read_profile(identity).configuration.traits.get('requires_ami') is True
                       for _, identity in self.candidate.profiles)
                   or _routes_need_ami(candidate, self.manager.catalog_loader(candidate)))
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
