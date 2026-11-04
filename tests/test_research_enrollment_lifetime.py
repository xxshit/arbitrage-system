"""The sleeping history worker must not keep completed research generations alive."""
import ast
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


class Profile(dict):
    pass


class CheckAtIdle(BaseException):
    pass


class ResearchEnrollmentLifetimeTests(unittest.TestCase):
    def check_idle(self, fail):
        state = dict(in_context=False, checked_out=False, enrollments=0)
        references, warnings, checkpoints = [], [], []

        def noop(*args):
            pass

        @contextmanager
        def app_context():
            state['in_context'] = True
            try:
                yield
            finally:
                state['in_context'] = False

        def build(snapshot, limit, hours, *, for_validation):
            self.assertTrue(state['in_context'] and for_validation)
            state['checked_out'] = True
            profile = Profile(windows={'15d': {'history': [1, 2]}})
            references.append(weakref.ref(profile))
            return [{'symbol': 'SYN/USDT', 'activity': profile}]

        def remove():
            state['checked_out'] = False

        def enroll(rows, now):
            self.assertFalse(state['in_context'] or state['checked_out'])
            self.assertIs(references[0](), rows[0]['activity'])
            state['enrollments'] += 1
            if fail:
                raise ValueError('synthetic enrollment failure')

        def idle(_):
            self.assertEqual(state['enrollments'], 1)
            self.assertIsNone(references[0](), 'idle worker retains the completed profile')
            raise CheckAtIdle()

        ledger = SimpleNamespace(ready=True, observe=noop, persist=noop, enroll=enroll)
        ns = dict(time=SimpleNamespace(time=lambda: 1000., monotonic=lambda: 1., sleep=idle),
                  app=SimpleNamespace(app_context=app_context, logger=SimpleNamespace(
                      exception=lambda *args: self.fail('unexpected writer failure'), warning=lambda *args: warnings.append(args))),
                  db=SimpleNamespace(session=SimpleNamespace(remove=remove)), OPPORTUNITY_VALIDATION=ledger,
                  OPPORTUNITY_PROFILES=SimpleNamespace(refresh=noop), OPPORTUNITY_ACTIVITY=SimpleNamespace(ready=True, persist=noop),
                  OPPORTUNITY_BOOKS=SimpleNamespace(persist=noop, dropped=0), SPREAD_HISTORY_BUFFER=SimpleNamespace(dropped=0),
                  SPREAD_HISTORY_STORE=SimpleNamespace(prune=noop), BACKGROUND_RUNTIME=SimpleNamespace(progress=checkpoints.append),
                  persist_spread_history=noop, refresh_opportunity_quality=noop, load_latest_market_snapshot=lambda: {},
                  spot_thinking_candidates=build)
        # Load just the worker's functions, without importing Flask application startup.
        source = Path(__file__).resolve().parents[1] / 'app.py'
        names = {'background_spread_history', 'enroll_opportunity_candidates'}
        functions = [v for v in ast.parse(source.read_text()).body if isinstance(v, ast.FunctionDef) and v.name in names]
        exec(compile(ast.Module(functions, []), str(source), 'exec'), ns)
        with self.assertRaises(CheckAtIdle):
            ns['background_spread_history']()
        self.assertEqual(checkpoints, ['success'])
        self.assertEqual(len(warnings), int(fail))

    def test_completed_registration_releases_profile_before_sleep(self):
        self.check_idle(False)

    def test_failed_registration_releases_profile_and_continues_history_work(self):
        self.check_idle(True)
