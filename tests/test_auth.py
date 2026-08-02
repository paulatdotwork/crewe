import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

# Point the user store at a throwaway file so the real ~/crewe_users.json is untouched.
tmpdir = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmpdir, "users.json")
router.app.config["SESSION_COOKIE_SECURE"] = False   # test client speaks http

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))

c = router.app.test_client()

print("--- unauthenticated ---")
r = c.get("/", headers={"Accept": "text/html"})
check("GET / redirects to /login", r.status_code == 302 and "/login" in r.headers.get("Location",""), f"{r.status_code} {r.headers.get('Location')}")
r = c.get("/login")
check("GET /login is public (200)", r.status_code == 200, str(r.status_code))
check("login page renders 'COMING SOON'", b"COMING SOON" in r.data)
r = c.post("/ask", json={"question": "hi"})
check("POST /ask (XHR) -> 401 JSON not redirect", r.status_code == 401 and r.is_json, str(r.status_code))
r = c.get("/docs/list", headers={"Accept": "application/json"})
check("GET /docs/list -> 401", r.status_code == 401, str(r.status_code))
r = c.get("/healthz")
check("GET /healthz is public", r.status_code == 200, str(r.status_code))

print("--- account creation ---")
u, err = router.create_user("nobody@example.com", "short", is_admin=False)
check("rejects short password", u is None and err is not None, str(err))
u, err = router.create_user("not-an-email", "longenoughpassword", is_admin=False)
check("rejects invalid email", u is None and err is not None, str(err))
admin, err = router.create_user("admin@example.com", "correct-horse-battery", is_admin=True)
check("creates admin", admin is not None and err is None, str(err))
plain, err = router.create_user("user@example.com", "correct-horse-battery", is_admin=False)
check("creates normal user", plain is not None and err is None, str(err))
dupe, err = router.create_user("admin@example.com", "correct-horse-battery")
check("rejects duplicate email", dupe is None and err is not None, str(err))
stored = json.load(open(router.USERS_FILE))["users"]
check("password is not stored in plaintext", all("correct-horse-battery" not in json.dumps(x) for x in stored))

print("--- login ---")
r = c.post("/login", data={"email": "admin@example.com", "password": "wrong"})
check("wrong password -> 401", r.status_code == 401, str(r.status_code))
r = c.post("/login", data={"email": "ghost@example.com", "password": "whatever"})
check("unknown email -> 401 (same as wrong pw)", r.status_code == 401, str(r.status_code))
r = c.post("/login", data={"email": "admin@example.com", "password": "correct-horse-battery"})
check("correct password -> redirect to /", r.status_code == 302 and r.headers.get("Location","").endswith("/"), f"{r.status_code} {r.headers.get('Location')}")

print("--- authenticated as ADMIN ---")
r = c.get("/", headers={"Accept": "text/html"})
check("GET / now 200", r.status_code == 200, str(r.status_code))
r = c.get("/settings", headers={"Accept": "text/html"})
check("admin reaches /settings", r.status_code == 200, str(r.status_code))

print("--- authenticated as NON-ADMIN ---")
c2 = router.app.test_client()
c2.post("/login", data={"email": "user@example.com", "password": "correct-horse-battery"})
r = c2.get("/", headers={"Accept": "text/html"})
check("non-admin reaches /", r.status_code == 200, str(r.status_code))
r = c2.get("/settings", headers={"Accept": "text/html"})
check("non-admin BLOCKED from /settings (403)", r.status_code == 403, str(r.status_code))
r = c2.post("/settings/config", json={"x": 1})
check("non-admin BLOCKED from POST /settings/config", r.status_code == 403, str(r.status_code))

print("--- logout ---")
c2.get("/logout")
r = c2.get("/", headers={"Accept": "text/html"})
check("after logout, / redirects again", r.status_code == 302, str(r.status_code))

print(f"\n{len(P)} passed, {len(F)} failed")
if F:
    print("FAILED: " + ", ".join(F)); sys.exit(1)
