"""Normal-call kernel counters cannot retain payloads or change error semantics."""
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

import background_runtime as runtime

REAL_CURRENT_THREAD = threading.current_thread


class MemoryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = []
        for name, value in (
            ('_MEMORY_PHASE_COUNTS', {}), ('_MEMORY_PHASE_DROPPED', 0),
            ('_MEMORY_PHASE_LOCAL', SimpleNamespace(identity=(123, 400))),
        ):
            item = patch.object(runtime, name, value)
            item.start()
            self.addCleanup(item.stop)
        item = patch.object(runtime.threading, 'current_thread', return_value=SimpleNamespace(name='spot-market-refresh'))
        item.start()
        self.addCleanup(item.stop)

    def test_counts_are_inclusive_and_totals_are_detached(self):
        @runtime.memory_phase('market_json_decode')
        def decode():
            return 'unchanged'
        @runtime.memory_phase('market_batch')
        def batch():
            return decode()
        with patch.object(runtime, '_phase_sample', side_effect=[(10,1), (12,1.1), (16,1.2), (20,1.4)]):
            self.assertEqual(batch(), 'unchanged')
        output = runtime.memory_phase_totals()
        phases = {r['phase']:r for r in output['rows']}
        self.assertEqual(phases['market_json_decode']['minor_faults'], 4)
        self.assertEqual(phases['market_batch']['minor_faults'], 10)
        phases['market_batch']['calls'] = 999
        self.assertEqual(runtime.memory_phase_totals()['rows'][-1]['calls'], 1)

    def test_unlisted_threads_do_not_measure_or_expose_name(self):
        with patch.object(runtime.threading, 'current_thread', return_value=SimpleNamespace(name='private-user-value')), \
             patch.object(runtime, '_phase_sample') as sampler:
            self.assertEqual(runtime.memory_phase('market_batch')(lambda: 7)(), 7)
            sampler.assert_not_called()
        self.assertNotIn('private-user-value', json.dumps(runtime.memory_phase_totals()))

    def test_arguments_and_return_value_are_not_retained(self):
        class Payload:
            pass
        @runtime.memory_phase('spot_snapshot')
        def identity(payload):
            return payload
        payload = Payload()
        witness = weakref.ref(payload)
        with patch.object(runtime, '_phase_sample', return_value=(1,1)):
            result = identity(payload)
        self.assertIs(result, payload)
        del result, payload
        self.assertIsNone(witness())

    def test_exception_and_shutdown_propagate_unchanged(self):
        for cls in (ValueError, KeyboardInterrupt, SystemExit):
            error = cls('test-only-sensitive-value')
            @runtime.memory_phase('market_batch')
            def failing():
                raise error
            with patch.object(runtime, '_phase_sample', side_effect=[(1,1), (3,2)]):
                with self.assertRaises(cls) as caught:
                    failing()
            self.assertIs(caught.exception, error)
        self.assertNotIn('test-only-sensitive-value', json.dumps(runtime.memory_phase_totals()))
        self.assertEqual(runtime.memory_phase_totals()['rows'][0]['calls'], 3)

    def test_unavailable_kernel_counters_preserve_result(self):
        for samples in ([None], [(1,1), None]):
            with patch.object(runtime, '_phase_sample', side_effect=samples):
                self.assertEqual(runtime.memory_phase('market_batch')(lambda: 9)(), 9)
        self.assertEqual(runtime.memory_phase_totals()['rows'], [])

    def test_thread_start_ticks_prevent_tid_reuse_merge(self):
        wrapped = runtime.memory_phase('quote_confirm')(lambda: None)
        with patch.object(runtime, '_phase_sample', return_value=(1,1)):
            wrapped()
            runtime._MEMORY_PHASE_LOCAL.identity = (123, 999)
            wrapped()
        self.assertEqual({r['start_ticks'] for r in runtime.memory_phase_totals()['rows']}, {400,999})

    def test_cap_is_visible_and_never_suppresses_target(self):
        with patch.object(runtime, '_MEMORY_PHASE_LIMIT', 0), \
             patch.object(runtime, '_phase_sample', return_value=(1,1)):
            self.assertEqual(runtime.memory_phase('market_batch')(lambda: 8)(), 8)
            self.assertEqual(runtime.memory_phase_totals(), {'rows':[], 'limit':0, 'dropped_calls':1})

    def test_fixed_names_only_and_no_resource_counter_on_unsupported_platform(self):
        with self.assertRaises(ValueError):
            runtime.memory_phase('unreviewed label')
        with patch.object(runtime.resource, 'getrusage', side_effect=OSError):
            self.assertIsNone(runtime._phase_sample())

    def test_real_thread_local_state_does_not_keep_threads_alive(self):
        # The fixed role skips counters on unsupported platforms, yet must not
        # keep a Thread or its arguments in a global diagnostics registry.
        with patch.object(runtime, '_MEMORY_PHASE_LOCAL', threading.local()), \
             patch.object(runtime.threading, 'current_thread', new=REAL_CURRENT_THREAD):
            thread = threading.Thread(name='spot-market-refresh', daemon=True,
                                      target=runtime.memory_phase('market_batch')(lambda: None))
            witness = weakref.ref(thread)
            thread.start()
            thread.join()
            del thread
        self.assertIsNone(witness())


if __name__ == '__main__':
    unittest.main()
