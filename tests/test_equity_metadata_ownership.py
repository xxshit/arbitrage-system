"""Exact wire/owner boundary and failures; AST only, no app bootstrap or DB."""
import ast
from concurrent.futures import Future, ThreadPoolExecutor
import gzip
import io
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request
import weakref

from market_transport import decode_json
from background_runtime import memory_phase

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('ARBI_METADATA_SOURCE', ROOT/'app.py'))
KEY = 'equity_bybit_instruments'
URL = 'https://api.bybit.com/v5/market/instruments-info?category=spot'


class Response:
    def __init__(self, body, encoding='gzip', error=None):
        self.body=body;self.headers={'Content-Encoding':encoding};self.closed=False;self.error=error;self.read_sizes=[]
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def read(self,size):
        self.read_sizes.append(size)
        if self.error:raise self.error
        return self.body


class Payload(dict):pass


def environment():
    nodes=[n for n in ast.parse(SOURCE.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in
           ('get_json','fetch_equity_metadata','collect_equity_metadata')]
    assert len(nodes)==3
    env=dict(Request=Request,HTTPError=HTTPError,urlopen=Mock(),MARKET_HTTP=SimpleNamespace(context=object()),
             time=SimpleNamespace(monotonic=lambda:100.),decode_json=decode_json,memory_phase=memory_phase,
             EQUITY_METADATA_POOL=Mock(),EQUITY_METADATA_PENDING={},EQUITY_METADATA_ERRORS={},EQUITY_METADATA_RETRY_AT={},
             MARKET_PAYLOAD_CACHE={},EQUITY_SPOT_URLS={KEY:URL},EQUITY_SPOT_TTLS={KEY:60})
    env['EQUITY_METADATA_POOL'].submit.side_effect=lambda *args:Future()
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),'exec'),env)
    return env


class EquityMetadataOwnershipTests(unittest.TestCase):
    def setUp(self):self.env=environment()

    def future(self,document,observed=99.):
        future=Future();future.set_result((document,observed));self.env['EQUITY_METADATA_PENDING'][KEY]=future;return future

    def test_network_worker_transfers_wire_without_decoding(self):
        body=gzip.compress(b'{"retCode":0,"result":{"list":[{"value":1.23456789012345}]}}')
        response=Response(body);self.env['urlopen'].return_value=response
        decode=Mock(wraps=decode_json);self.env['decode_json']=decode
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.env['fetch_equity_metadata'],URL)
            self.assertEqual(future.result(timeout=2),((body,'gzip'),100.))
        self.assertEqual(decode.call_count,0)
        self.assertTrue(response.closed)
        self.assertEqual(response.read_sizes,[16*1024*1024+1])
        call=self.env['urlopen'].call_args
        self.assertEqual(call.kwargs,{'timeout':3,'context':self.env['MARKET_HTTP'].context})
        self.assertEqual(call.args[0].headers['Accept-encoding'],'gzip')

    def test_full_values_decode_only_on_cache_owner_with_original_observation_time(self):
        value={'retCode':0,'result':{'list':[{'name':'股票😀','price':'1.23000','value':1.23456789012345,'large':2**60}]}}
        body=gzip.compress(json.dumps(value,ensure_ascii=False).encode())
        self.future((body,'gzip'),42.)
        owners=[]
        def decode(*args):owners.append(threading.get_ident());return decode_json(*args)
        self.env['decode_json']=decode
        result,errors=self.env['collect_equity_metadata']()
        self.assertEqual(result,{KEY:value});self.assertEqual(errors,{})
        self.assertEqual(owners,[threading.get_ident()])
        self.assertEqual(self.env['MARKET_PAYLOAD_CACHE'][URL]['fetched_at'],42.)

    def test_pending_jobs_never_block_or_duplicate_and_keep_cached_values(self):
        old={'old':True};self.env['MARKET_PAYLOAD_CACHE'][URL]=dict(payload=old,fetched_at=1.)
        pending=Future();self.env['EQUITY_METADATA_PENDING'][KEY]=pending
        for _ in range(3):
            values,_=self.env['collect_equity_metadata']();self.assertIs(values[KEY],old)
        self.env['EQUITY_METADATA_POOL'].submit.assert_not_called()

    def test_replaced_tree_releases_but_an_existing_reader_stays_valid(self):
        old=Payload(value='old');ref=weakref.ref(old)
        self.env['MARKET_PAYLOAD_CACHE'][URL]=dict(payload=old,fetched_at=1.)
        reader={KEY:old};del old
        self.future((b'{"value":"new"}',''))
        values,errors=self.env['collect_equity_metadata']()
        self.assertEqual(values,{KEY:{'value':'new'}});self.assertEqual(errors,{})
        self.assertEqual(reader[KEY],{'value':'old'})
        del reader
        self.assertIsNone(ref())

    def test_bad_json_gzip_and_encoding_preserve_old_value_and_retry_gate(self):
        broken=gzip.compress(b'{"ok":true}')[:-4]
        for document in ((b'{broken',''),(broken,'gzip'),(b'{}','br'),(b'\xff','')):
            with self.subTest(encoding=document[1],body_bytes=len(document[0])):
                env=environment();old=Payload(old=True);env['MARKET_PAYLOAD_CACHE'][URL]=dict(payload=old,fetched_at=1.)
                f=Future();f.set_result((document,99.));env['EQUITY_METADATA_PENDING'][KEY]=f
                values,errors=env['collect_equity_metadata']()
                self.assertIs(values[KEY],old);self.assertIn(KEY,errors)
                self.assertEqual(env['MARKET_PAYLOAD_CACHE'][URL]['fetched_at'],1.)
                self.assertEqual(env['EQUITY_METADATA_RETRY_AT'][KEY],105.)
                env['EQUITY_METADATA_POOL'].submit.assert_not_called()
                env['time'].monotonic=lambda:105.
                env['collect_equity_metadata']();env['EQUITY_METADATA_POOL'].submit.assert_called_once()

    def test_expanded_payload_limit_remains_enforced_on_owner(self):
        old={'old':True};self.env['MARKET_PAYLOAD_CACHE'][URL]=dict(payload=old,fetched_at=1.)
        self.future((gzip.compress(b'"'+b'a'*(16*1024*1024)+b'"'),'gzip'))
        result,errors=self.env['collect_equity_metadata']()
        self.assertIs(result[KEY],old);self.assertEqual(errors,{KEY:'ValueError'})

    def test_wire_limit_and_read_failure_close_response(self):
        class Oversize:
            def __len__(self):return 16*1024*1024+1
        for response in (Response(Oversize()),Response(b'',error=TimeoutError())):
            self.env['urlopen'].return_value=response
            with self.assertRaises((ValueError,TimeoutError)):self.env['fetch_equity_metadata'](URL)
            self.assertTrue(response.closed)

    def test_http_error_response_is_closed_before_future_propagation(self):
        stream=io.BytesIO(b'synthetic rejection')
        error=HTTPError(URL,503,'synthetic',{},stream)
        self.env['urlopen'].side_effect=error
        with self.assertRaises(HTTPError):self.env['fetch_equity_metadata'](URL)
        self.assertTrue(stream.closed)

    def test_failed_future_does_not_keep_consumer_traceback(self):
        future=Future();error=TimeoutError();future.set_exception(error)
        self.env['EQUITY_METADATA_PENDING'][KEY]=future
        _,errors=self.env['collect_equity_metadata']()
        self.assertEqual(errors,{KEY:'TimeoutError'});self.assertIsNone(error.__traceback__)

    def test_price_refresh_and_metadata_worker_bound_are_unchanged(self):
        values={n.targets[0].id:n.value for n in ast.parse(SOURCE.read_text()).body
                if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)}
        self.assertEqual(ast.literal_eval(values['MARKET_REFRESH_SECONDS']),5)
        self.assertEqual(ast.literal_eval(values['EQUITY_MARKET_REFRESH_SECONDS']),15)
        self.assertEqual(next(ast.literal_eval(k.value) for k in values['EQUITY_METADATA_POOL'].keywords if k.arg=='max_workers'),3)


if __name__=='__main__':unittest.main()
