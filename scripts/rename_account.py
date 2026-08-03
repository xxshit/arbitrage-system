"""Rename one website account and invalidate its active session."""

import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import AccountSecurityEvent, UserAccount, app, db  # noqa: E402


def rename_account(old_username, new_username, actor_username):
    old_username = str(old_username).strip().lower()
    new_username = str(new_username).strip().lower()
    actor_username = str(actor_username).strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,24}", new_username):
        raise ValueError("The new username must use 3-24 lowercase letters, digits, or underscores.")

    with app.app_context():
        actor = UserAccount.query.filter_by(username=actor_username, role="admin").first()
        if actor is None:
            raise ValueError("The requested administrator account was not found.")
        target = UserAccount.query.filter_by(username=old_username).first()
        if target is None:
            raise ValueError("The account to rename was not found.")
        collision = UserAccount.query.filter_by(username=new_username).first()
        if collision is not None and collision.id != target.id:
            raise ValueError("The new username is already in use.")

        target.username = new_username
        target.active_session_hash = None
        db.session.add(AccountSecurityEvent(
            actor_user_id=actor.id,
            target_user_id=target.id,
            event_type="admin_username_changed",
        ))
        db.session.commit()
        return {
            "account_id": target.id,
            "old_exists": UserAccount.query.filter_by(username=old_username).count(),
            "new_exists": UserAccount.query.filter_by(username=new_username).count(),
            "active_session_cleared": target.active_session_hash is None,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Rename one website account and invalidate its session."
    )
    parser.add_argument("old_username")
    parser.add_argument("new_username")
    parser.add_argument("--actor", required=True, help="Administrator username performing the change")
    args = parser.parse_args()
    result = rename_account(args.old_username, args.new_username, args.actor)
    print(
        "ACCOUNT_ID={account_id} OLD_EXISTS={old_exists} NEW_EXISTS={new_exists} "
        "ACTIVE_SESSION_CLEARED={active_session_cleared}".format(**result)
    )


if __name__ == "__main__":
    main()
