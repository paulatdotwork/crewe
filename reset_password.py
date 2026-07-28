#!/usr/bin/env python3
"""Reset a Crewe account password from the machine that runs the server.

    python3 reset_password.py you@example.com

Crewe sends no email, so there is no "forgot password" link and never will be
one that works offline. The operator of the box is the recovery path: if you
can edit files on the server, you can reset any password on it. That is the
whole security model — guard the box, not the reset.

The password is prompted for, never passed as an argument, so it stays out of
your shell history. Writes to ~/crewe_users.json atomically; no restart needed,
because the store is read per request.
"""
import getpass
import sys

# router.py owns the user store; import it rather than duplicating the schema.
# Importing pulls in its module-level config but starts no server (that is
# guarded by __main__), so this is safe to run while the router is live.
try:
    import router
except Exception as e:
    sys.exit(f"could not import router.py: {e}")

_load_users = router._load_users
_save_users = router._save_users
_user_by_email = router._user_by_email
generate_password_hash = router.generate_password_hash
MIN_PASSWORD_LEN = getattr(router, "MIN_PASSWORD_LEN", 10)


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit(__doc__)
    email = sys.argv[1].strip().lower()

    user = _user_by_email(email)
    if not user:
        known = [u.get("email", "") for u in _load_users()]
        sys.exit(f"no account for {email!r}\n"
                 f"accounts on this box: {', '.join(known) or '(none)'}")

    pw = getpass.getpass(f"New password for {email} (min {MIN_PASSWORD_LEN}): ")
    if len(pw) < MIN_PASSWORD_LEN:
        sys.exit(f"too short — minimum {MIN_PASSWORD_LEN} characters")
    if pw != getpass.getpass("Confirm: "):
        sys.exit("passwords did not match")

    users = _load_users()
    for u in users:
        if u.get("email") == email:
            u["pw_hash"] = generate_password_hash(pw)
            break
    _save_users(users)

    print(f"password reset for {email}")
    print("Sign in at /login — no restart needed.")
    print("Note: existing browser sessions stay valid; this only changes what "
          "the login form accepts.")


if __name__ == "__main__":
    main()
