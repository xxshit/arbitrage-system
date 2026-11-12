"""Short local subprocess controls: buffered final output and forced deadline."""
import importlib.util
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parent
def load(name):
    spec = importlib.util.spec_from_file_location(name, root / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
sampler, parser = load('bounded-sampler'), load('frame-aggregate')
manifest = {'target_threads': {'123': 1}, 'sources': {
    'app.py': {'functions': ['background_spread_history'], 'lines': 17000}}}
line = 'thread (123): SECRET_NAME;SECRET_FRAME (private.py:1);background_spread_history (app.py:15484) 7'
child_code = 'import time,sys\ntime.sleep(.15)\ntime.sleep(.2)\nprint(' + repr(line) + ')\nprint("SECRET_STDERR",file=sys.stderr)\n'
good = sampler.collect([sys.executable, '-c', child_code], parser.Aggregator(manifest), parser.MAX_LINE, lambda: None, deadline_seconds=1)
assert good['sampler_exit'] == 0 and good['abort'] is None
assert good['aggregate']['frames'] == [dict(tid=123,file='app.py',function='background_spread_history',line=15484,count=7)]
assert good['pipe_counts']['stderr_bytes'] > 0 and 'SECRET' not in json.dumps(good)
bad = sampler.collect([sys.executable, '-c', child_code], parser.Aggregator(manifest), parser.MAX_LINE, lambda: None, deadline_seconds=.1)
assert bad['sampler_exit'] != 0 and bad['abort'] == 'bounded_wall_clock_end'
assert not bad['aggregate']['frames']
began = time.monotonic()
def failing_guard():
    if time.monotonic() - began > .1:
        raise RuntimeError('SECRET_GUARD')
gated = sampler.collect([sys.executable, '-c', 'import time;time.sleep(10)'], parser.Aggregator(manifest), parser.MAX_LINE, failing_guard, deadline_seconds=1)
assert gated['sampler_exit'] != 0 and gated['abort'] == 'identity_resource_or_quote_gate'
assert 'SECRET' not in json.dumps(gated)
print(json.dumps({'status': 'pass', 'delayed_final_output': good, 'deadline_before_final_output': bad, 'guard_failure': gated}, indent=2))
