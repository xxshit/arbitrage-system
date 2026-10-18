#!/usr/bin/env python3
"""Owner-operated, hidden-input provisioning. No network or trading functions."""
import argparse
import fcntl
import getpass
import grp
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings

PROVIDERS = ("Binance", "Bybit", "OKX")
CONFIG_PATH = Path("/etc/arbiscope/transfer-readonly.json")


class ConfigurationError(Exception):
    """Deliberately has no credential-bearing error messages."""


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "命令参数无效；不要把密钥放在命令行，请使用隐藏输入。\n")


def validate_credentials(exchange, credentials):
    expected = {"api_key", "api_secret"} | ({"passphrase"} if exchange == "OKX" else set())
    if exchange not in PROVIDERS or not isinstance(credentials, dict) or set(credentials) != expected:
        raise ConfigurationError()
    for value in credentials.values():
        if not isinstance(value, str) or not 8 <= len(value) <= 1024 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ConfigurationError()


def secure_file_info(info, owner_uid):
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o027:
        raise ConfigurationError()


def load_config(path, owner_uid, group_gid=None):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {"version": 1, "exchanges": {}}
    try:
        info = os.fstat(descriptor)
        secure_file_info(info, owner_uid)
        if group_gid is not None and info.st_gid != group_gid:
            raise ConfigurationError()
        if info.st_size > 32768:
            raise ConfigurationError()
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            result = json.load(stream)
        if not isinstance(result, dict) or set(result) != {"version", "exchanges"} or result["version"] != 1 or not isinstance(result["exchanges"], dict):
            raise ConfigurationError()
        for exchange, credentials in result["exchanges"].items():
            validate_credentials(exchange, credentials)
        return result
    except Exception:
        raise ConfigurationError() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def ensure_directory(path, owner_uid, group_gid):
    try:
        os.mkdir(path, 0o750)
        os.chown(path, owner_uid, group_gid)
        os.chmod(path, 0o750)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner_uid or info.st_gid != group_gid or stat.S_IMODE(info.st_mode) & 0o027:
        raise ConfigurationError()


def save_credentials(path, exchange, credentials, owner_uid, group_gid, replace=False):
    validate_credentials(exchange, credentials)
    ensure_directory(path.parent, owner_uid, group_gid)
    lock_fd = os.open(path.parent / ".transfer-readonly.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    temporary = None
    try:
        secure_file_info(os.fstat(lock_fd), owner_uid)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        config = load_config(path, owner_uid, group_gid)
        if exchange in config["exchanges"] and not replace:
            raise ConfigurationError()
        config["exchanges"][exchange] = dict(credentials)
        descriptor, temporary = tempfile.mkstemp(prefix=".transfer-readonly-", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), owner_uid, group_gid)
            os.fchmod(stream.fileno(), 0o640)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)
        os.close(lock_fd)


def hidden_input(label):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(label)


def main():
    parser = PrivateArgumentParser(description="只读充提密钥私密输入；不调用任何交易所接口，不接受命令行密钥")
    parser.add_argument("exchange", nargs="?", choices=PROVIDERS)
    parser.add_argument("--status", action="store_true", help="只显示是否配置，不显示任何密钥")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ConfigurationError()
        group_gid = grp.getgrnam("arbhub").gr_gid
        if CONFIG_PATH.parent.exists() or CONFIG_PATH.parent.is_symlink():
            ensure_directory(CONFIG_PATH.parent, 0, group_gid)
        config = load_config(CONFIG_PATH, 0, group_gid)
        if args.status:
            for exchange in PROVIDERS:
                print(f"{exchange}: {'已配置（不代表已接通或验证权限）' if exchange in config['exchanges'] else '尚未配置'}")
            return 0
        if not args.exchange or not sys.stdin.isatty():
            raise ConfigurationError()
        print("本工具不创建API、不核验交易所实际权限，也不调用交易/划转/提现或余额接口。")
        if input("已在交易所确认仅读取、关闭交易/划转/提现、IP仅47.238.145.158？输入 YES 继续：") != "YES":
            return 1
        replacing = args.exchange in config["exchanges"]
        if replacing and input("此交易所已配置。确认替换？输入 REPLACE：") != "REPLACE":
            return 1
        credentials = {"api_key": hidden_input("API Key（隐藏输入）："), "api_secret": hidden_input("Secret（隐藏输入）：")}
        if credentials["api_secret"] != hidden_input("再次输入 Secret（隐藏输入）："):
            raise ConfigurationError()
        if args.exchange == "OKX":
            credentials["passphrase"] = hidden_input("Passphrase（隐藏输入）：")
        save_credentials(CONFIG_PATH, args.exchange, credentials, 0, group_gid, replace=replacing)
        print("已保存到服务器私密配置；未发送至交易所，未验证权限或接通状态，未重启网站。")
        return 0
    except (Exception, KeyboardInterrupt):
        print("操作未完成或保存结果未确认；不会显示密钥。请检查输入/权限，或联系协助。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
