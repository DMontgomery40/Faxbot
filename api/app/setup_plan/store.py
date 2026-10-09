"""Setup plans in the database: each preview, and each time one was applied (migration 0053).

Both tables only grow: a plan is inserted once and never changed, and each
apply inserts one row saying what it did. A plan's number is the next number
in one serialized write transaction.
"""
import hashlib
import json
import uuid

import sqlalchemy as sa

from ..routing.database import read_connection, reflect, utcnow, write_transaction


TABLES = ('setup_plans', 'setup_plan_applications')


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)


def digest(text):
    return hashlib.sha256(text.encode('ascii')).hexdigest()


class PlanStore:
    def __init__(self, engine):
        self.engine = engine
        tables = reflect(engine, TABLES)
        self.plans, self.applications = tables['setup_plans'], tables['setup_plan_applications']

    def create(self, *, basis, context, plan, actor_principal_id=None, actor_name=None, now=None):
        text = encode(plan)
        row = {'id': uuid.uuid4().hex, 'basis': basis, 'context': encode(context), 'plan': text,
               'digest': digest(text), 'actor_principal_id': actor_principal_id,
               'actor_name': (str(actor_name)[:200] if actor_name else None), 'created_at': now or utcnow()}
        with write_transaction(self.engine) as connection:
            number = connection.execute(sa.select(sa.func.coalesce(sa.func.max(self.plans.c.number), 0))
                                        ).scalar_one() + 1
            connection.execute(self.plans.insert().values(number=number, **row))
        return self.get(number)

    @staticmethod
    def _plan(row):
        if row is None:
            return None
        found = dict(row)
        found['context'] = json.loads(found['context'])
        found['plan'] = json.loads(found['plan'])
        return found

    def get(self, number):
        with read_connection(self.engine) as connection:
            return self._plan(connection.execute(sa.select(self.plans).where(self.plans.c.number == number)
                                                 ).mappings().one_or_none())

    def latest(self):
        with read_connection(self.engine) as connection:
            return self._plan(connection.execute(sa.select(self.plans).order_by(self.plans.c.number.desc()).limit(1)
                                                 ).mappings().one_or_none())

    def recent(self, limit=20):
        columns = [column for column in self.plans.c if column.name not in ('plan', 'context')]
        with read_connection(self.engine) as connection:
            return [dict(row) for row in connection.execute(
                sa.select(*columns).order_by(self.plans.c.number.desc()).limit(limit)).mappings()]

    def applications_of(self, plan_id):
        with read_connection(self.engine) as connection:
            rows = connection.execute(sa.select(self.applications).where(self.applications.c.plan_id == plan_id)
                                      .order_by(self.applications.c.created_at, self.applications.c.id)).mappings()
            found = []
            for row in rows:
                item = dict(row)
                item['items'], item['steps'] = json.loads(item['items']), json.loads(item['steps'])
                found.append(item)
            return found

    def record_application(self, plan_id, *, outcome, items, steps, restart_required, actor_principal_id=None,
                           actor_name=None, now=None):
        row = {'id': uuid.uuid4().hex, 'plan_id': plan_id, 'outcome': outcome, 'items': encode(list(items)),
               'steps': encode(list(steps)), 'restart_required': 1 if restart_required else 0,
               'actor_principal_id': actor_principal_id,
               'actor_name': (str(actor_name)[:200] if actor_name else None), 'created_at': now or utcnow()}
        with write_transaction(self.engine) as connection:
            connection.execute(self.applications.insert().values(**row))
        return row
