"""Exercise actual named-lock cleanup without importing app or sending messages."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

REPO = Path(__file__).resolve().parents[1]


def load_lock_functions(source=None):
    if source is None:
        source = (REPO / 'app.py').read_text()
    names = {'acquire_thought_push_lock', 'release_thought_push_lock', 'send_thought_analysis_push'}
    selected = [node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    if len(selected) != len(names):
        raise AssertionError('missing real lock function')
    namespace = {'text': lambda statement: statement}
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<thought-lock-functions>', 'exec'), namespace)
    return namespace


class Interrupted(BaseException):
    pass


class SimulatedConnection:
    def __init__(self, failure=None, acquired=1):
        self.failure = failure
        self.acquired = acquired
        self.owned = False
        self.closed = 0
        self.invalidated = 0

    def execute(self, statement):
        if 'GET_LOCK' in statement:
            self.owned = self.acquired == 1
            if self.failure == 'acquire':
                raise TimeoutError('lost acquire response')
            if self.failure == 'interrupt':
                raise Interrupted('interrupted acquire')
            def scalar():
                if self.failure == 'scalar':
                    raise RuntimeError('unreadable acquire result')
                return self.acquired
            return SimpleNamespace(scalar=scalar)
        if 'RELEASE_LOCK' not in statement:
            raise AssertionError('unexpected query')
        if self.failure == 'release':
            raise RuntimeError('unlock failed')
        self.owned = False
        return SimpleNamespace(scalar=lambda: 1)

    def invalidate(self):
        self.invalidated += 1
        self.owned = False

    def close(self):
        # A pooled close does not release a MySQL named lock by itself.
        self.closed += 1


class ThoughtPushLockTests(unittest.TestCase):
    source = None

    def setup_connection(self, failure=None, acquired=1, dialect='mysql'):
        self.ns = load_lock_functions(self.source)
        self.connection = SimulatedConnection(failure, acquired)
        self.body_calls = 0
        self.connect_calls = 0
        def connect():
            self.connect_calls += 1
            return self.connection
        def body(**kwargs):
            self.body_calls += 1
            return True
        self.ns.update(db=SimpleNamespace(engine=SimpleNamespace(
            dialect=SimpleNamespace(name=dialect), connect=connect)),
            send_thought_analysis_push_locked=body)

    def assert_discarded(self):
        self.assertEqual(self.connection.invalidated, 1)
        self.assertEqual(self.connection.closed, 1)
        self.assertFalse(self.connection.owned)
        self.assertEqual(self.body_calls, 0)

    def test_acquire_failure_discards_connection(self):
        self.setup_connection('acquire')
        with self.assertRaises(TimeoutError):
            self.ns['send_thought_analysis_push']()
        self.assert_discarded()

    def test_scalar_failure_discards_connection(self):
        self.setup_connection('scalar')
        with self.assertRaises(RuntimeError):
            self.ns['send_thought_analysis_push']()
        self.assert_discarded()

    def test_interrupted_acquire_discards_connection(self):
        self.setup_connection('interrupt')
        with self.assertRaises(Interrupted):
            self.ns['send_thought_analysis_push']()
        self.assert_discarded()

    def test_release_failure_discards_possibly_locked_connection(self):
        self.setup_connection('release')
        with self.assertRaises(RuntimeError):
            self.ns['send_thought_analysis_push']()
        self.assertEqual(self.connection.invalidated, 1)
        self.assertEqual(self.connection.closed, 1)
        self.assertFalse(self.connection.owned)
        self.assertEqual(self.body_calls, 1)

    def test_success_keeps_lock_until_body_finishes(self):
        self.setup_connection()
        def body(**kwargs):
            self.assertTrue(self.connection.owned)
            self.assertEqual(self.connection.closed, 0)
            self.body_calls += 1
            return True
        self.ns['send_thought_analysis_push_locked'] = body
        self.assertTrue(self.ns['send_thought_analysis_push'](only_symbols=set()))
        self.assertEqual(self.body_calls, 1)
        self.assertEqual(self.connection.closed, 1)
        self.assertEqual(self.connection.invalidated, 0)
        self.assertFalse(self.connection.owned)

    def test_busy_lock_skips_body_and_closes(self):
        self.setup_connection(acquired=0)
        self.assertFalse(self.ns['send_thought_analysis_push']())
        self.assertEqual(self.body_calls, 0)
        self.assertEqual(self.connection.closed, 1)
        self.assertEqual(self.connection.invalidated, 0)

    def test_body_error_still_unlocks_and_closes(self):
        self.setup_connection()
        def body(**kwargs):
            raise ValueError('synthetic body failure')
        self.ns['send_thought_analysis_push_locked'] = body
        with self.assertRaises(ValueError):
            self.ns['send_thought_analysis_push']()
        self.assertFalse(self.connection.owned)
        self.assertEqual(self.connection.closed, 1)
        self.assertEqual(self.connection.invalidated, 0)

    def test_non_mysql_path_keeps_existing_behavior(self):
        self.setup_connection(dialect='sqlite')
        self.assertTrue(self.ns['send_thought_analysis_push']())
        self.assertEqual(self.body_calls, 1)
        self.assertEqual(self.connect_calls, 0)


if __name__ == '__main__':
    unittest.main()
