"""Allowlisted frame counts only; raw lines, order, values and names are dropped.

The sampler uses a pinned py-spy binary. This parser has no process access.
"""
import json
import re
from collections import Counter

MAX_LINE = 131072
THREAD = re.compile(r'thread \(([0-9]{1,20}|0[xX][0-9A-Fa-f]{1,16})\)(?:: [^;\r\n]*)?\Z')
FRAME = re.compile(r'([^;\r\n]{1,240}) \(([^;\r\n]{1,1024}):([0-9]{1,7})\)\Z')


class Aggregator:
    def __init__(self, manifest):
        self.manifest = manifest
        self.frames = Counter()
        self.counts = Counter()

    def add(self, raw):
        self.counts['lines'] += 1
        if len(raw) > MAX_LINE or '\x00' in raw:
            self.counts['invalid'] += 1
            return
        stack, sep, weight = raw.rstrip('\n').rpartition(' ')
        if not sep or not weight.isascii() or not weight.isdecimal() or not 0 < int(weight) <= 100000:
            self.counts['invalid'] += 1
            return
        parts = stack.split(';')
        match = THREAD.fullmatch(parts[0])
        if not match or len(parts) > 1024:
            self.counts['invalid'] += 1
            return
        value = match[1]
        tid = int(value, 16 if value.lower().startswith('0x') else 10)
        if str(tid) not in self.manifest['target_threads']:
            self.counts['other_thread_samples'] += int(weight)
            return
        self.counts['target_thread_samples'] += int(weight)
        for part in parts[1:]:
            frame = FRAME.fullmatch(part)
            if not frame:
                self.counts['unknown_frames'] += int(weight)
                continue
            function, location, line = frame.groups()
            if location.startswith('/opt/arbitrage-hub/'):
                location = location[len('/opt/arbitrage-hub/'):]
            source = self.manifest['sources'].get(location)
            if (source is None or function not in source['functions']
                    or not 1 <= int(line) <= source['lines']):
                self.counts['unknown_frames'] += int(weight)
                continue
            self.frames[(tid, location, function, int(line))] += int(weight)

    def result(self):
        return {'counts': dict(self.counts), 'frames': [
            dict(tid=t, file=f, function=fn, line=line, count=n)
            for (t, f, fn, line), n in sorted(self.frames.items())]}


def privacy_check():
    manifest = {'target_threads': {'123': 1}, 'sources': {
        'app.py': {'functions': ['background_spread_history'], 'lines': 17000}}}
    agg = Aggregator(manifest)
    agg.add('thread (123): SECRET_THREAD;SECRET_ACCOUNT (secret.py:1);background_spread_history (/opt/arbitrage-hub/app.py:15484) 2\n')
    agg.add('thread (0x7b);background_spread_history (app.py:15484) 3\n')
    agg.add('thread (123);SECRET_SQL (app.py:15484) 1\n')
    agg.add('thread (999);background_spread_history (app.py:15484) 5\n')
    agg.add('thread (123);background_spread_history (/other/app.py:15484) 1\n')
    agg.add('thread (123);background_spread_history (app.py:999999) 1\n')
    agg.add('SECRET_LINE' * MAX_LINE)
    agg.add('thread (123);background_spread_history (app.py:1) 0\n')
    result = agg.result()
    assert result['frames'] == [dict(tid=123, file='app.py', function='background_spread_history', line=15484, count=5)]
    assert 'SECRET' not in json.dumps(result) and '/opt/' not in json.dumps(result)
    return {'checks': 8, 'status': 'pass', 'raw_input_saved': False}


if __name__ == '__main__':
    print(json.dumps(privacy_check()))
