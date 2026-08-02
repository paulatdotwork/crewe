import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmp = tempfile.mkdtemp()
router.USERS_FILE    = os.path.join(tmp, "users.json")
router.USERDATA_ROOT = os.path.join(tmp, "userdata")
router.app.config["SESSION_COOKIE_SECURE"] = False

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))

alice, _ = router.create_user("alice@example.com", "correct-horse-battery")
bob,   _ = router.create_user("bob@example.com",   "correct-horse-battery")

ca, cb = router.app.test_client(), router.app.test_client()
ca.post("/login", data={"email": "alice@example.com", "password": "correct-horse-battery"})
cb.post("/login", data={"email": "bob@example.com",   "password": "correct-horse-battery"})

print("--- storage roots are distinct ---")
router.set_owner(alice["id"]); a_docs = router.docs_dir(); a_mem = router.memory_file()
router.set_owner(bob["id"]);   b_docs = router.docs_dir(); b_mem = router.memory_file()
check("docs_dir differs per user", a_docs != b_docs, f"{a_docs} vs {b_docs}")
check("memory_file differs per user", a_mem != b_mem)
check("alice's root contains her uid", alice["id"] in a_docs)

print("--- accessors refuse to guess outside a request ---")
router.set_owner(None)
try:
    router.docs_dir(); check("docs_dir raises with no owner", False, "did not raise")
except RuntimeError:
    check("docs_dir raises with no owner", True)
try:
    router.sessions(); check("sessions() raises with no owner", False, "did not raise")
except RuntimeError:
    check("sessions() raises with no owner", True)

print("--- documents are isolated ---")
r = ca.post("/docs/new", json={}, headers={"Accept": "application/json"})
did = (r.get_json() or {}).get("id") if r.is_json else None
check("alice creates a doc", r.status_code in (200, 201) and did, f"{r.status_code} {r.data[:80]}")
if did:
    ca.post(f"/docs/save/{did}", json={"title": "Alice Secret", "html": "<p>hers</p>"})
    la = ca.get("/docs/list", headers={"Accept": "application/json"}).get_json()
    lb = cb.get("/docs/list", headers={"Accept": "application/json"}).get_json()
    sa = json.dumps(la); sb = json.dumps(lb)
    check("alice sees her doc", "Alice Secret" in sa, sa[:120])
    check("bob does NOT see alice's doc", "Alice Secret" not in sb, sb[:120])
    rb = cb.get(f"/docs/get/{did}", headers={"Accept": "application/json"})
    body = rb.get_json() if rb.is_json else {}
    check("bob cannot fetch alice's doc by id",
          rb.status_code >= 400 or "Alice Secret" not in json.dumps(body),
          f"{rb.status_code} {json.dumps(body)[:120]}")

print("--- chat sessions are isolated ---")
router.set_owner(alice["id"])
router.sessions()["shared-id"] = {"history": [["q", "alice private answer"]]}
router.set_owner(bob["id"])
bob_view = router.sessions().get("shared-id")
check("bob's sessions lack alice's session_id (client-supplied id no longer leaks)",
      bob_view is None, str(bob_view)[:100])

print("--- cookbook is isolated ---")
router.set_owner(alice["id"]); router.cookbook().append({"id": "c1", "title": "Alice Pie"})
router.set_owner(bob["id"])
check("bob's cookbook is empty", router.cookbook() == [], str(router.cookbook())[:80])

print("--- jobs are owner-scoped ---")
router.set_owner(alice["id"])
with router.JOBS_LOCK:
    router.JOBS["job-a"] = {"owner": alice["id"], "tokens": 0, "done": True,
                            "route": "general", "answer": "alice's answer"}
router.set_owner(alice["id"])
check("alice can read her job", router.owned_job(router.JOBS, "job-a") is not None)
router.set_owner(bob["id"])
check("bob CANNOT read alice's job", router.owned_job(router.JOBS, "job-a") is None)
r = cb.get("/progress/job-a", headers={"Accept": "application/json"})
check("bob's /progress on alice's job -> 404", r.status_code == 404, str(r.status_code))
r = ca.get("/progress/job-a", headers={"Accept": "application/json"})
check("alice's /progress on her job -> 200", r.status_code == 200, str(r.status_code))

print("--- spawn_owned propagates the owner into threads ---")
import threading as _t
seen = {}
def _worker():
    try: seen["uid"] = router._owner()
    except RuntimeError as e: seen["err"] = str(e)
router.set_owner(alice["id"])
t = router.spawn_owned(_worker); t.start(); t.join(5)
check("worker thread inherits alice's uid", seen.get("uid") == alice["id"], str(seen))
seen.clear()
t = _t.Thread(target=_worker); t.start(); t.join(5)
check("bare threading.Thread has NO owner (fails loudly, not silently)", "err" in seen, str(seen))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
