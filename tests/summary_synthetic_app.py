"""Guarded AST loader for local synthetic checks, never a normal app import.

Must run in a disposable subprocess: it forbids network sockets and threads,
ignores dotenv, skips schema/seed bootstrap, and redirects the instance directory.
"""
import ast
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types


def load(source):
    source = Path(source).resolve()
    private = Path(tempfile.mkdtemp(prefix='arbiscope-summary-check-'))
    (private/'instance').mkdir(mode=0o700)
    os.environ.update(PYTHON_DOTENV_DISABLED='1', DATABASE_URL='sqlite://', SECRET_KEY='synthetic-only',
                      CHAT_ENCRYPTION_KEY='AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=', TZ='UTC')
    os.chdir(private)
    time.tzset()
    class LocalSocket(socket.socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family != socket.AF_UNIX:
                raise AssertionError('synthetic summary check forbids network')
            super().__init__(family, *args, **kwargs)
    socket.socket = LocalSocket
    def no_threads(*args, **kwargs):
        raise AssertionError('synthetic summary check forbids workers')
    threading.Thread.start = no_threads
    import flask
    class LocalFlask(flask.Flask):
        def __init__(self, *args, **kwargs):
            kwargs['instance_path'] = str(private/'instance')
            super().__init__(*args, **kwargs)
    flask.Flask = LocalFlask
    tree = ast.parse(source.read_text())
    bootstrap = [n for n in tree.body if isinstance(n, ast.With) and any(
        isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id=='create_database_schema'
        for v in ast.walk(n))]
    dotenv = [n for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
              and isinstance(n.value.func, ast.Name) and n.value.func.id=='load_dotenv']
    assert len(bootstrap)==len(dotenv)==1
    tree.body = [n for n in tree.body if n not in bootstrap+dotenv]
    module = types.ModuleType('app')
    module.__file__ = str(source)
    sys.modules['app'] = module
    exec(compile(tree, str(source), 'exec'), module.__dict__)
    assert not module.BACKGROUND_WORKERS_STARTED
    assert module.MARKET_REFRESH_SECONDS == 5
    assert {r['value'] for r in module.RUNTIME_RULE_DEFAULTS
            if r['rule_key'] in ('spot_market_refresh','dual_market_refresh','browser_alert_refresh')} == {'5'}
    with module.app.app_context():
        assert str(module.db.engine.url) == 'sqlite://'
    return module, private
