"""Create the private chat-at-rest encryption key without printing it."""

import argparse
import base64
import os
import secrets
from pathlib import Path


def ensure_key(path: Path) -> bool:
    path = path.expanduser().resolve()
    if path.exists():
        value = path.read_text(encoding="ascii").strip()
        try:
            decoded = base64.urlsafe_b64decode(value.encode("ascii"))
        except Exception as exc:
            raise SystemExit(f"Chat encryption key file is invalid: {path}") from exc
        if len(decoded) != 32:
            raise SystemExit(f"Chat encryption key must decode to exactly 32 bytes: {path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
        handle.write(value + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=".chat_encryption.key")
    args = parser.parse_args()
    created = ensure_key(Path(args.path))
    print("Chat encryption key created." if created else "Chat encryption key already configured.")


if __name__ == "__main__":
    main()
