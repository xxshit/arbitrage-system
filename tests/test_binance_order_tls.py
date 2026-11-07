"""Bounded loopback TLS wire test. Only synthetic keys and generated fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from binance_hedge.client import BinanceClient, OrderConnection, OrderGate


class LoopbackTLS(unittest.TestCase):
    def test_prepared_pair_uses_validated_tls_and_complete_signed_http_messages(self):
        received=[]; errors=[]; connections=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args): pass
            def do_POST(self):
                try:
                    body=self.rfile.read(int(self.headers['Content-Length'])).decode()
                    unsigned,signature=body.rsplit('&signature=',1)
                    assert hmac.compare_digest(signature,hmac.new(b'synthetic-secret',unsigned.encode(),hashlib.sha256).hexdigest())
                    assert self.headers['X-MBX-APIKEY']=='synthetic-key'
                    assert self.headers['Accept-Encoding']=='identity'
                    params={k:v[0] for k,v in parse_qs(unsigned).items()}
                    assert params['timeInForce']=='FOK' and params['recvWindow']=='2000'
                    assert self.path in ('/api/v3/order','/fapi/v1/order')
                    received.append((self.path,params['newClientOrderId'],time.monotonic()))
                    data=json.dumps(dict(symbol=params['symbol'],clientOrderId=params['newClientOrderId'],
                        side=params['side'],executedQty=params['quantity'],status='FILLED',orderId=101)).encode()
                    self.send_response(200)
                except Exception as exc:
                    errors.append(type(exc).__name__);data=b'{}';self.send_response(500)
                self.send_header('Content-Length',str(len(data)));self.send_header('Content-Type','application/json')
                self.end_headers();self.wfile.write(data)
        with tempfile.TemporaryDirectory(prefix='arbiscope-tls-fixture-') as tmp:
            key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
            subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'synthetic loopback')])
            now=datetime.now(timezone.utc)
            cert=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                  .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1))
                  .not_valid_after(now+timedelta(hours=1)).add_extension(x509.SubjectAlternativeName([
                      x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256()))
            root=Path(tmp);cp=root/'synthetic.crt';kp=root/'synthetic.key'
            cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            kp.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
            kp.chmod(0o600)
            server_tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);server_tls.load_cert_chain(cp,kp)
            servers=[];threads=[];client=BinanceClient('synthetic-key','synthetic-secret',allow_orders=True)
            client._tls=ssl.create_default_context(cafile=str(cp))
            client._offset={m:(0,time.monotonic()) for m in ('spot','futures')}
            original_create=socket.create_connection
            try:
                for _ in range(2):
                    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
                    server.socket=server_tls.wrap_socket(server.socket,server_side=True)
                    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.02},daemon=True)
                    thread.start();servers.append(server);threads.append(thread)
                class LocalConnection(OrderConnection):
                    def __init__(self,host,**kwargs):
                        self.test_market='spot' if host=='api.binance.com' else 'futures'; self.ready_at=None
                        super().__init__('127.0.0.1',port=servers[self.test_market=='futures'].server_port,**kwargs)
                        connections.append(self)
                    def connect(self):
                        if self.test_market=='futures':time.sleep(.2)
                        super().connect();self.ready_at=time.monotonic()
                def only_loopback(address,*args,**kwargs):
                    self.assertEqual(address[0],'127.0.0.1')
                    self.assertIn(address[1],[s.server_port for s in servers])
                    return original_create(address,*args,**kwargs)
                with patch('binance_hedge.client.OrderConnection',LocalConnection),patch.object(socket,'create_connection',only_loopback):
                    for index in range(2):
                        gate=OrderGate(time.monotonic()+1);timings={m:{} for m in ('spot','futures')}
                        def send(market):
                            return client.submit(market,dict(symbol='BTCUSDT',side='BUY' if market=='spot' else 'SELL',type='LIMIT',
                                timeInForce='FOK',price='100',quantity='.1',newClientOrderId=f'synthetic-{market}-{index}'),gate=gate,timing=timings[market])
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            jobs=[pool.submit(send,m) for m in ('spot','futures')]
                            results=[job.result(timeout=3) for job in jobs]
                        self.assertEqual([r['status'] for r in results],['FILLED','FILLED'])
                        self.assertGreaterEqual(min(t['write_started_monotonic'] for t in timings.values()),max(c.ready_at for c in connections))
                        for t in timings.values():self.assertGreaterEqual(t['write_completed_monotonic'],t['write_started_monotonic'])
                self.assertEqual(errors,[]);self.assertEqual(len(received),4);self.assertEqual(len(connections),2)
                self.assertEqual(len({r[1] for r in received}),4)
                self.assertTrue(client._tls.check_hostname);self.assertEqual(client._tls.verify_mode,ssl.CERT_REQUIRED)
            finally:
                client.close()
                for server in servers:server.shutdown();server.server_close()
                for thread in threads:thread.join(timeout=1)


if __name__=='__main__': unittest.main()
