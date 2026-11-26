#!/usr/bin/env python3
"""Reset an account password on this computer:  python reset_password.py USERNAME"""
import getpass, sys

import auth, config
from db import DB


def main():
    if len(sys.argv) != 2:
        print("Usage: python reset_password.py USERNAME"); return 1
    db = DB(config.DB_PATH); u = db.user_by_ident(sys.argv[1])
    if not u:
        print("No such account."); return 1
    pw = getpass.getpass("New password: ")
    errs = auth.validate_password(pw, u["username"], u["email"])
    if errs or pw != getpass.getpass("Repeat password: "):
        print("Not changed: " + (" ".join(errs) or "passwords do not match.")); return 1
    db.update_user(u["id"], pw_hash=auth.hash_password(pw)); db.del_user_auth(u["id"])
    print(f"Password changed for {u['username']}. All devices were signed out."); return 0


if __name__ == "__main__":
    sys.exit(main())
