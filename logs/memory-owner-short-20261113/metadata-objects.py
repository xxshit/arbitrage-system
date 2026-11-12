"""Offline public-fixture object sizes, not production heap inspection."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import sys
import tracemalloc
from types import SimpleNamespace
from datetime import datetime, timezone

SOURCE = Path('/opt/arbitrage-hub/market_transport.py')
SHA = '968f2b2c2daba7704a35ced5d631d9cb0496c2a4f6341418622ee52e74c30d21'


def main():
    os.nice(19);resource.setrlimit(resource.RLIMIT_AS,(256*1024**2,256*1024**2));resource.setrlimit(resource.RLIMIT_CPU,(12,12))
    sys.dont_write_bytecode=True
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest()==SHA
    spec=importlib.util.spec_from_file_location('pinned_transport',SOURCE)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    root=Path(sys.argv[1]);manifest=json.loads((root/'manifest.json').read_text());rows=[]
    result={'started_at_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':SHA,'rows':rows}
    for entry in manifest['directories']:
        wire=(root/(entry['key']+'.wire')).read_bytes()
        assert hashlib.sha256(wire).hexdigest()==entry['wire_sha256']
        row={'key':entry['key'],'wire_bytes':len(wire)}
        def loads(text):
            row.update(text_chars=len(text),text_object_bytes=sys.getsizeof(text),before_json_current=tracemalloc.get_traced_memory()[0])
            payload=json.loads(text)
            row.update(after_json_current=tracemalloc.get_traced_memory()[0],peak_during_json=tracemalloc.get_traced_memory()[1])
            return payload
        module.json=SimpleNamespace(loads=loads)
        tracemalloc.start()
        payload,size=module.decode_json(wire,entry['encoding'])
        row.update(decoded_bytes=size,live_after_return=tracemalloc.get_traced_memory()[0],peak_after_return=tracemalloc.get_traced_memory()[1])
        del payload
        row['live_after_release']=tracemalloc.get_traced_memory()[0]
        tracemalloc.stop();rows.append(row)
    result['ended_at_utc']=datetime.now(timezone.utc).isoformat()
    print(json.dumps(result))


if __name__=='__main__': main()
