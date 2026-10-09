"""Durable singleton queue. Short serialized transactions never encompass network I/O.

A five-minute lease exceeds the agent's two-minute hard deadline. Expired work
is marked failed, never automatically replayed: its remote billing is uncertain.
"""
from datetime import timedelta
import json
import hashlib
from uuid import uuid4
import sqlalchemy as sa
from ..routing.database import reflect, read_connection, write_transaction, utcnow


def configured(values):
    return bool(values.analysis_api_key and values.analysis_model)


def signature(values):
    # Never hash or store the API key. Endpoint/model changes make old advice stale.
    return hashlib.sha256(json.dumps([values.analysis_provider, values.analysis_base_url,
                                    values.analysis_model]).encode()).hexdigest()


def stamp(value):
    return value.isoformat() + 'Z' if value is not None else None


class AnalysisStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, ('analysis_state', 'analysis_runs'))
        self.state, self.runs = tables['analysis_state'], tables['analysis_runs']

    def _state(self, connection):
        row = connection.execute(sa.select(self.state)).mappings().first()
        if row is None:
            connection.execute(self.state.insert().values(id='installation', state='idle'))
            row = connection.execute(sa.select(self.state)).mappings().one()
        return row

    def queue(self, values, *, now=None):
        if not configured(values) or not values.analysis_enabled:
            raise ValueError('Configure and enable analysis before refreshing it.')
        with write_transaction(self.engine) as connection:
            row = self._state(connection)
            if row['state'] not in ('queued', 'running'):
                connection.execute(self.state.update().values(state='queued', message=None))
        return self.status(values, now=now)

    def claim(self, values, *, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = self._state(connection)
            active = configured(values) and values.analysis_enabled
            if not active:
                if row['state'] == 'running':
                    connection.execute(self.runs.update().where(self.runs.c.id == row['current_run_id']).values(
                        finished_at=now, error='Analysis was disabled or its configuration was removed.'))
                if row['state'] != 'idle' or row['next_run_at'] is not None:
                    connection.execute(self.state.update().values(state='idle', lease_until=None, next_run_at=None,
                                                                   current_run_id=None, message=None))
                return None
            if row['state'] == 'running':
                if row['lease_until'] <= now:
                    message = 'The previous analysis was interrupted. Refresh it to try again.'
                    connection.execute(self.runs.update().where(self.runs.c.id == row['current_run_id']).values(
                        finished_at=now, error=message))
                    connection.execute(self.state.update().values(state='failed', lease_until=None, message=message))
                return None
            interval = values.analysis_interval_hours
            next_run = row['next_run_at']
            if interval and row['current_run_id']:
                started = connection.execute(sa.select(self.runs.c.started_at).where(
                    self.runs.c.id == row['current_run_id'])).scalar_one_or_none()
                if started:
                    next_run = started + timedelta(hours=interval)
                    if next_run != row['next_run_at']:
                        connection.execute(self.state.update().values(next_run_at=next_run))
            due = bool(interval and (next_run is None or next_run <= now))
            if row['state'] != 'queued' and not due:
                if not interval and row['next_run_at'] is not None:
                    connection.execute(self.state.update().values(next_run_at=None))
                return None
            identity = uuid4().hex
            entry = dict(id=identity, started_at=now, provider=values.analysis_provider,
                         model=values.analysis_model, config_signature=signature(values), evidence='[]', usage='{}')
            connection.execute(self.runs.insert().values(**entry))
            connection.execute(self.state.update().values(state='running', current_run_id=identity,
                lease_until=now + timedelta(minutes=5), message=None,
                next_run_at=now + timedelta(hours=interval) if interval else None))
            return entry

    def finish(self, run, *, summary=None, evidence=None, usage=None, error=None, now=None):
        now = now or utcnow()
        with write_transaction(self.engine) as connection:
            row = self._state(connection)
            if row['state'] != 'running' or row['current_run_id'] != run['id'] or row['lease_until'] <= now:
                return False
            connection.execute(self.runs.update().where(self.runs.c.id == run['id']).values(
                finished_at=now, summary=summary, evidence=json.dumps(evidence or []),
                usage=json.dumps(usage or {}), error=error))
            changes = dict(state='failed' if error else 'succeeded', lease_until=None, message=error)
            if not error:
                changes['last_success_id'] = run['id']
            connection.execute(self.state.update().values(**changes))
            return True

    def status(self, values, *, now=None):
        now = now or utcnow()
        with read_connection(self.engine) as connection:
            row = connection.execute(sa.select(self.state)).mappings().first()
            last = None
            stale_config = False
            aged = False
            if row:
                identity = row['last_success_id'] or row['current_run_id']
                if identity:
                    run = connection.execute(sa.select(self.runs).where(self.runs.c.id == identity)).mappings().first()
                    if run:
                        last = dict(run)
                        stale_config = last.pop('config_signature') != signature(values)
                        aged = run['started_at'] + timedelta(hours=values.analysis_interval_hours or 24) <= now
                        for name in ('started_at', 'finished_at'):
                            last[name] = stamp(last[name])
                        for name in ('evidence', 'usage'):
                            last[name] = json.loads(last[name])
        ready = configured(values)
        state = 'not_configured' if not ready else 'disabled' if not values.analysis_enabled else row['state'] if row else 'idle'
        next_run = row['next_run_at'] if row and ready and values.analysis_enabled and values.analysis_interval_hours else None
        stale = (last is None or last['summary'] is None or state in ('queued', 'running', 'failed')
                 or stale_config or aged or bool(next_run and next_run <= now))
        return dict(configured=ready, enabled=values.analysis_enabled, state=state,
                    message=row['message'] if row else None, last_run=last, next_run_at=stamp(next_run), stale=stale)
