import os, sys, tempfile, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmp = tempfile.mkdtemp()
router.USERS_FILE    = os.path.join(tmp, "users.json")
router.INVITES_FILE  = os.path.join(tmp, "invites.json")
router.USERDATA_ROOT = os.path.join(tmp, "userdata")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))

admin, _ = router.create_user("admin@x.com", "correct-horse-battery", is_admin=True)
plain, _ = router.create_user("plain@x.com", "correct-horse-battery", is_admin=False)

adm = router.app.test_client(); adm.post("/login", data={"email":"admin@x.com","password":"correct-horse-battery"})
usr = router.app.test_client(); usr.post("/login", data={"email":"plain@x.com","password":"correct-horse-battery"})
anon = router.app.test_client()

print("--- access control on new pages ---")
check("admin reaches /admin", adm.get("/admin").status_code == 200)
check("non-admin BLOCKED from /admin (403)", usr.get("/admin", headers={"Accept":"application/json"}).status_code == 403)
check("anon redirected from /admin", anon.get("/admin", headers={"Accept":"text/html"}).status_code == 302)
check("both reach /account", adm.get("/account").status_code == 200 and usr.get("/account").status_code == 200)
check("whoami admin flag true", adm.get("/whoami").get_json().get("is_admin") is True)
check("whoami non-admin flag false", usr.get("/whoami").get_json().get("is_admin") is False)

print("--- change password ---")
r = usr.post("/account/password", data={"current":"wrong","new":"new-good-password","confirm":"new-good-password"})
check("wrong current pw -> flashed error, no change", router.check_password_hash(router._user_by_email("plain@x.com")["pw_hash"], "correct-horse-battery"))
r = usr.post("/account/password", data={"current":"correct-horse-battery","new":"short","confirm":"short"})
check("too-short new pw rejected", router.check_password_hash(router._user_by_email("plain@x.com")["pw_hash"], "correct-horse-battery"))
r = usr.post("/account/password", data={"current":"correct-horse-battery","new":"mismatchA1234","confirm":"mismatchB1234"})
check("mismatched confirm rejected", router.check_password_hash(router._user_by_email("plain@x.com")["pw_hash"], "correct-horse-battery"))
r = usr.post("/account/password", data={"current":"correct-horse-battery","new":"brand-new-password","confirm":"brand-new-password"})
check("correct change applied", router.check_password_hash(router._user_by_email("plain@x.com")["pw_hash"], "brand-new-password"))
# and can log in with the new password
c2 = router.app.test_client()
check("can log in with changed password", c2.post("/login", data={"email":"plain@x.com","password":"brand-new-password"}).status_code == 302)

print("--- invite creation (admin only) ---")
r = usr.post("/admin/invite", data={"email":"friend@x.com"})   # non-admin
check("non-admin cannot create invite (403)", r.status_code == 403)
r = adm.post("/admin/invite", data={"email":"friend@x.com","is_admin":"1"})
check("admin invite -> redirect", r.status_code == 302)
invs = router._load_invites()
check("invite persisted", len(invs) == 1 and invs[0]["email"] == "friend@x.com")
check("invite carries admin flag", invs[0]["is_admin"] is True)
check("token is long/urlsafe", len(invs[0]["token"]) >= 32)
token = invs[0]["token"]
r = adm.post("/admin/invite", data={"email":"admin@x.com"})   # already has account
check("invite for existing email is refused", len(router._load_invites()) == 1)

print("--- invite acceptance (public, token-gated) ---")
check("bad token -> 410", anon.get("/invite/not-a-real-token").status_code == 410)
r = anon.get(f"/invite/{token}")
check("valid token page loads for anon (no login)", r.status_code == 200 and b"friend@x.com" in r.data)
r = anon.post(f"/invite/{token}", data={"password":"invited-user-pw","confirm":"invited-user-pw"})
check("accept -> redirect to / (auto login)", r.status_code == 302 and r.headers.get("Location","").endswith("/"))
newu = router._user_by_email("friend@x.com")
check("invited account created", newu is not None)
check("invited account got admin flag from invite", newu and newu.get("is_admin") is True)
check("invited user can log in", router.app.test_client().post("/login", data={"email":"friend@x.com","password":"invited-user-pw"}).status_code == 302)

print("--- invite is single-use ---")
check("token now consumed -> 410 on reuse", anon.get(f"/invite/{token}").status_code == 410)
r2 = router.app.test_client().post(f"/invite/{token}", data={"password":"another-pw12","confirm":"another-pw12"})
check("cannot reuse token to POST", r2.status_code == 410)

print("--- expired invite ---")
inv2, _ = router.create_invite("later@x.com", False, "admin@x.com")
allinv = router._load_invites()
for i in allinv:
    if i["token"] == inv2["token"]: i["expires"] = 1  # in the past
router._save_invites(allinv)
check("expired token -> 410", anon.get(f"/invite/{inv2['token']}").status_code == 410)

print("--- user management guards ---")
r = adm.post("/admin/user/delete", data={"uid": admin["id"]})  # self
check("admin cannot delete self", router._user_by_email("admin@x.com") is not None)
# make friend the only OTHER admin, demote... actually test last-admin guard:
# currently admins: admin@x.com and friend@x.com. Remove friend (ok), then admin is last.
friend = router._user_by_email("friend@x.com")
adm.post("/admin/user/delete", data={"uid": friend["id"]})
check("admin can remove another user", router._user_by_email("friend@x.com") is None)
# now admin@x.com is the last admin; deleting via a second admin is impossible, so
# simulate: there is only one admin left, guard should refuse if we tried to remove them
users = router._load_users()
admins = [u for u in users if u.get("is_admin")]
check("exactly one admin remains", len(admins) == 1)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
