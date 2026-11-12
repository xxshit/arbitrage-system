"""Private local hints; the database remains the only source of work."""
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat


def socket_path():
    return Path(os.getenv("BINANCE_EXECUTOR_LOCK", "/run/arbitrage-binance/executor.lock")).with_name("wake.sock")


def notify():
    try:
        path = socket_path()
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.02)
            sock.sendto(b"work", str(path))
    except OSError:
        pass  # lost notification is covered by the existing 1-second DB scan


@contextmanager
def listener():
    path = socket_path()
    # Caller owns the exclusive executor flock before touching this socket.
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Invalid executor notification socket")
        path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.settimeout(1)
        try:
            yield sock
        finally:
            path.unlink(missing_ok=True)


def wait(sock):
    try:
        sock.recv(16)
    except socket.timeout:
        pass
