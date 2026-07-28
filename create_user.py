#!/usr/bin/env python3
"""Create a Crewe account. Public signup is Phase 3 — until then accounts are
made here.

    python3 create_user.py alice@example.com
    python3 create_user.py pal@example.com --admin
    python3 create_user.py --list

Password is prompted for (never passed as an argument, which would land it in
your shell history). Writes to ~/crewe_users.json.
"""
import argparse
import getpass
import sys

# router.py owns the user store; import it rather than duplicating the schema.
# Importing pulls in its module-level config but starts no server (that is
# guarded by __main__), so this is safe to run while the router is live.
try:
    from router import create_user, _load_users
except Exception as e:
    sys.exit(f"could not import router.py: {e}")


def main():
    ap = argparse.ArgumentParser(description="Create a Crewe account.")
    ap.add_argument("email", nargs="?", help="account email address")
    ap.add_argument("--admin", action="store_true",
                    help="grant admin (access to /settings, which holds API keys)")
    ap.add_argument("--list", action="store_true", help="list existing accounts")
    args = ap.parse_args()

    if args.list:
        users = _load_users()
        if not users:
            print("no accounts yet")
            return
        print(f"{'EMAIL':38} {'ADMIN':6} CREATED")
        for u in users:
            print(f"{u.get('email',''):38} {'yes' if u.get('is_admin') else '-':6} "
                  f"{u.get('created','')}")
        return

    if not args.email:
        ap.error("email required (or use --list)")

    pw = getpass.getpass("Password (min 10 chars): ")
    if pw != getpass.getpass("Confirm password: "):
        sys.exit("passwords did not match")

    user, err = create_user(args.email, pw, is_admin=args.admin)
    if err:
        sys.exit(f"error: {err}")

    print(f"created {user['email']}" + ("  [admin]" if user["is_admin"] else ""))
    print("They can sign in at /login — no router restart needed.")


if __name__ == "__main__":
    main()
