"""Real application cleanup and SQLAlchemy QueuePool, simulated named locks.

No app import, database server, sockets, account state or message delivery.
This checks discard/return behavior, not MySQL server or production incidence.
"""
import ast
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.pool import QueuePool


class Interrupted(BaseException):
    pass


class PhysicalConnection:
    def __init__(self, owner):
        self.owner = owner
        self.held = False
        self.closed = False
        owner.live[id(self)] = self
        owner.created += 1

    def rollback(self):
        # MySQL named locks survive transaction rollback.
        pass

    def close(self):
        self.held = False
        self.closed = True
        self.owner.live.pop(id(self), None)
        self.owner.discarded += 1


class Lease:
    def __init__(self, owner):
        self.owner = owner
        self.connection = owner.pool.connect()
        self.physical = self.connection.dbapi_connection

    def execute(self, statement):
        owner = self.owner
        if 'GET_LOCK' in statement:
            self.physical.held = owner.mode != 'busy'
            if owner.mode == 'acquire':
                raise TimeoutError('synthetic lost acquire response')
            if owner.mode == 'interrupt':
                raise Interrupted('synthetic interrupted acquire')
            def scalar():
                if owner.mode == 'scalar':
                    raise RuntimeError('synthetic unreadable result')
                return int(self.physical.held)
            return SimpleNamespace(scalar=scalar)
        assert 'RELEASE_LOCK' in statement
        if owner.mode == 'release':
            raise RuntimeError('synthetic unlock failure')
        self.physical.held = False
        return SimpleNamespace(scalar=lambda: 1)

    def invalidate(self):
        self.connection.invalidate()

    def close(self):
        self.connection.close()


class ThoughtLockWorkload:
    def __init__(self, repo, source=None):
        path = Path(repo) / 'app.py'
        names = {'acquire_thought_push_lock', 'release_thought_push_lock',
                 'send_thought_analysis_push'}
        selected = [node for node in ast.parse(source if source is not None else path.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(selected) == len(names)
        self.live = {}
        self.created = self.discarded = self.cycles = self.body_calls = 0
        self.cases = Counter()
        self.mode = None
        self.pool = QueuePool(lambda: PhysicalConnection(self), pool_size=1,
                              max_overflow=0, timeout=1)
        engine = SimpleNamespace(dialect=SimpleNamespace(name='mysql'),
                                 connect=lambda: Lease(self))
        self.ns = dict(db=SimpleNamespace(engine=engine), text=lambda statement: statement,
                       send_thought_analysis_push_locked=self.body)
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), 'exec'), self.ns)

    def body(self, **kwargs):
        assert self.pool.checkedout() == 1
        assert sum(connection.held for connection in self.live.values()) == 1
        self.body_calls += 1
        if self.mode == 'body':
            raise ValueError('synthetic body failure')
        return True

    def step(self):
        for mode in ('success', 'busy', 'acquire', 'scalar', 'interrupt', 'release', 'body'):
            self.mode = mode
            expected = dict(acquire=TimeoutError, scalar=RuntimeError, interrupt=Interrupted,
                            release=RuntimeError, body=ValueError).get(mode)
            try:
                result = self.ns['send_thought_analysis_push'](only_symbols=set())
            except (Exception, Interrupted) as error:
                assert expected is not None and type(error) is expected, type(error).__name__
            else:
                assert expected is None, 'expected injected failure was swallowed'
                assert result is (mode == 'success')
            assert self.pool.checkedout() == 0, 'failed named-lock path retained a checkout'
            assert not any(connection.held for connection in self.live.values()), 'locked physical connection returned to pool'
            self.cases[mode] += 1
        self.cycles += 1

    def status(self):
        return dict(cycles=self.cycles, cases=dict(self.cases), checked_out=self.pool.checkedout(),
                    retained_named_locks=sum(connection.held for connection in self.live.values()),
                    physical_created=self.created, physical_discarded=self.discarded,
                    synthetic_body_calls=self.body_calls, external_requests=0)

    def verify(self):
        assert self.cycles > 0
        assert len(self.cases) == 7 and all(count == self.cycles for count in self.cases.values())
        assert self.discarded == 4*self.cycles
        assert self.body_calls == 3*self.cycles
        assert self.pool.checkedout() == 0 and not any(x.held for x in self.live.values())
        return dict(self.status(), exact_cleanup=True, server='simulated', pool='real SQLAlchemy QueuePool')

    def close(self):
        self.pool.dispose()
