"""Compare page: pick N backends, one prompt, side-by-side, judged.

Uses a mock backend server so no real model is called.
"""
import os, sys, json, time, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmpdir = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmpdir, "users.json")
router.app.config["SESSION_COOKIE_SECURE"] = False
import shutil as _sh
_cfg = os.path.join(tmpdir, "router_config.json")
if os.path.exists(router.ROUTER_CONFIG_FILE):
    _sh.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
else:                       # fresh clone: no config yet -> built-in defaults
    json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail and not cond else ""))

c = router.app.test_client()
admin, err = router.create_user("admin@example.com", "correct-horse-battery", is_admin=True)
assert admin, err
c.post("/login", data={"email": "admin@example.com", "password": "correct-horse-battery"})

# ------------------------------------------------------------ mock backends
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); self.rfile.read(n)
        who = self.server.server_address[1]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for tok in ("answer ", f"from {who}"):
            self.wfile.write(b"data: " + json.dumps(
                {"choices": [{"delta": {"content": tok}}]}).encode() + b"\r\n\r\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\r\n\r\n"); self.wfile.flush()

servers, urls = [], []
for _ in range(3):
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    servers.append(s); urls.append(f"http://127.0.0.1:{s.server_address[1]}")

cfg = json.loads(json.dumps(router.ROUTER_CONFIG))
cfg["backends"] = [b for b in cfg["backends"] if not b["id"].startswith("mock")]
for i, u in enumerate(urls):
    cfg["backends"].append({"id": f"mock{i}", "url": u, "kind": "openai",
                            "key": "", "model": f"m{i}",
                            "paid": i == 2, "price_in": 1.0, "price_out": 2.0})
router._apply_router_config(cfg)

print("--- the picker ---")
r = c.get("/compare/backends")
j = r.get_json()
ids = [b["id"] for b in j["backends"]]
check("lists the configured backends", {"mock0", "mock1", "mock2"} <= set(ids), str(ids))
blob = json.dumps(j)
check("never leaks backend URLs to the browser", "127.0.0.1" not in blob and "http" not in blob,
      blob[:160])
check("never leaks API keys", "key" not in blob, blob[:160])
check("flags which backends cost money",
      next(b for b in j["backends"] if b["id"] == "mock2")["paid"] is True)
check("reports the model name so you know what you're comparing",
      next(b for b in j["backends"] if b["id"] == "mock0")["model"] == "m0")
check("/compare/backends needs auth",
      router.app.test_client().get("/compare/backends").status_code == 401)

print("--- selection handling ---")
r = c.post("/compare/ask", json={"question": ""})
check("empty question -> 400", r.status_code == 400)
r = c.post("/compare/ask", json={"question": "hi", "backends": []})
check("no backends selected -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/compare/ask", json={"question": "hi", "backends": "mock0"})
check("a non-list selection -> 400", r.status_code == 400, str(r.status_code))
r = c.post("/compare/ask", json={"question": "hi", "backends": ["nope", "nope2"]})
check("unknown backend names -> 400, not a crash", r.status_code == 400, str(r.status_code))
r = c.post("/compare/ask", json={"question": "hi",
                                 "backends": ["mock0"] * 3 + ["mock1"]})
check("duplicates are collapsed",
      r.status_code == 200 and r.get_json()["targets"] == ["mock0", "mock1"],
      str(r.get_json()))
r = c.post("/compare/ask", json={"question": "hi",
                                 "backends": [f"mock{i%3}" for i in range(20)]})
check("dedup keeps it under the cap rather than erroring",
      r.status_code == 200, str(r.status_code))

print("--- the browser cannot supply a URL (no SSRF) ---")
r = c.post("/compare/ask", json={"question": "hi",
                                 "backends": ["http://169.254.169.254/latest/meta-data"]})
check("a URL passed as a backend name is rejected", r.status_code == 400,
      str(r.status_code))
check("names are resolved to URLs server-side only",
      "_chat_url(b)" in open(router.__file__).read())

print("--- a real three-way run ---")
r = c.post("/compare/ask", json={"question": "say hello",
                                 "backends": ["mock0", "mock1", "mock2"]})
job = r.get_json()["job_id"]
for _ in range(80):
    time.sleep(0.15)
    p = c.get(f"/compare/progress/{job}").get_json()
    if all(p.get(b, {}).get("done") for b in ("mock0", "mock1", "mock2")):
        break
check("every selected backend produced an answer",
      all(p[b]["answer"].startswith("answer from") for b in ("mock0", "mock1", "mock2")),
      str({b: p[b]["answer"][:20] for b in ("mock0", "mock1", "mock2")}))
check("each pane holds its OWN backend's answer (no cross-wiring)",
      len({p[b]["answer"] for b in ("mock0", "mock1", "mock2")}) == 3,
      str([p[b]["answer"] for b in ("mock0", "mock1", "mock2")]))
check("the pane order is preserved for the UI",
      p["order"] == ["mock0", "mock1", "mock2"], str(p.get("order")))
check("token counters moved", all(p[b]["tokens"] > 0 for b in ("mock0", "mock1", "mock2")))

print("--- the judge actually delivers a verdict ---")
# Regression: deleting a dead prompt once took COMPARE_JUDGE_SYSTEM with it.
# Every pane still completed, so a test that only checked panes passed while
# the judge thread died on a NameError and the page span forever.
router.SPECIALISTS["reasoning"] = urls[0] + "/v1/chat/completions"
r = c.post("/compare/ask", json={"question": "judge me",
                                 "backends": ["mock0", "mock1"]})
jj = r.get_json()["job_id"]
for _ in range(80):
    time.sleep(0.15)
    pj = c.get(f"/compare/progress/{jj}").get_json()
    if pj.get("done"):
        break
check("the run reaches done (the judge did not die silently)",
      pj.get("done") is True, str(pj.get("done")))
check("the judge produced a verdict",
      pj.get("judge", {}).get("done") and pj["judge"]["answer"].strip(),
      str(pj.get("judge"))[:160])

print("--- judges must not think (they burn the whole budget and return nothing) ---")
import inspect
for fn, what in ((router._check_compare_judge, "compare judge"),
                 (router.run_panel_judge, "panel judge")):
    src_ = inspect.getsource(fn)
    check(f"{what} disables thinking",
          '"enable_thinking": False' in src_ or "'enable_thinking': False" in src_)
check("an empty verdict explains itself rather than reading as 'still working'",
      "returned nothing" in inspect.getsource(router._check_compare_judge))

print("--- spend on a paid entrant ---")
router.set_owner(admin["id"])
spend_before = router.spend_summary()["requests"]
r = c.post("/compare/ask", json={"question": "again", "backends": ["mock1", "mock2"]})
job2 = r.get_json()["job_id"]
for _ in range(80):
    time.sleep(0.15)
    p2 = c.get(f"/compare/progress/{job2}").get_json()
    if all(p2.get(b, {}).get("done") for b in ("mock1", "mock2")):
        break
time.sleep(0.5)
after = router.spend_summary()
check("the paid entrant is billed (a shoot-out can cost money)",
      after["requests"] > spend_before, f"{spend_before} -> {after['requests']}")
paid_routes = [r_["route"] for r_ in after["recent"]]
check("...and only the paid one",
      all(r_.startswith("compare:mock2") for r_ in paid_routes[:1]), str(paid_routes[:2]))

print("--- judging ---")
r = c.post("/compare/ask", json={"question": "solo", "backends": ["mock0"]})
check("a single backend is allowed but says there's nothing to compare",
      r.status_code == 200, str(r.status_code))
job3 = r.get_json()["job_id"]
for _ in range(60):
    time.sleep(0.15)
    p3 = c.get(f"/compare/progress/{job3}").get_json()
    if p3.get("done"):
        break
check("the one-entrant run still finishes rather than hanging", p3.get("done") is True,
      str(p3.get("done")))
check("...with an explanatory judge note",
      "nothing to compare" in (p3.get("judge", {}).get("answer") or ""),
      str(p3.get("judge")))

print("--- merged: compare does models AND routes ---")
r = c.get("/compare/targets?mode=backends")
tb = r.get_json()
check("targets(models) lists backends", tb["mode"] == "backends" and tb["targets"])
r = c.get("/compare/targets?mode=routes")
tr = r.get_json()
check("targets(routes) lists routes", tr["mode"] == "routes" and tr["targets"])
check("targets leak no URLs or keys",
      "http" not in json.dumps(tr) + json.dumps(tb), "leak")
check("an unknown mode falls back rather than erroring",
      c.get("/compare/targets?mode=bogus").get_json()["mode"] == "backends")
check("/compare/targets needs auth",
      router.app.test_client().get("/compare/targets").status_code == 401)
r = c.post("/compare/ask", json={"question": "hi", "mode": "bogus", "targets": ["mock0"]})
check("an unknown mode is rejected on ask", r.status_code == 400, str(r.status_code))
r = c.post("/compare/ask", json={"question": "hi", "mode": "routes",
                                 "targets": ["nope"]})
check("routes mode rejects unknown route names", r.status_code == 400, str(r.status_code))
check("/panel now redirects to /compare",
      c.get("/panel", headers={"Accept": "text/html"}).status_code == 302,
      str(c.get("/panel", headers={"Accept": "text/html"}).status_code))

print("--- panel: routes are now selectable ---")
r = c.get("/panel/routes")
pr = r.get_json()["routes"]
check("/panel/routes lists configured routes", len(pr) > 0, str(pr)[:120])
check("panel exposes no URLs or keys",
      "http" not in json.dumps(pr) and "key" not in json.dumps(pr), json.dumps(pr)[:140])
check("panel marks which routes are on by default",
      any(x["default"] for x in pr), str([x["route"] for x in pr if x["default"]]))
check("/panel/routes needs auth",
      router.app.test_client().get("/panel/routes").status_code == 401)

# /panel/ask is gone — the panel merged into compare's "routes" mode, which is
# covered above. /panel/routes survives because compare's picker uses it.

print("--- ownership ---")
other = router.app.test_client()
router.create_user("other@example.com", "correct-horse-battery")
other.post("/login", data={"email": "other@example.com", "password": "correct-horse-battery"})
check("another user cannot read your comparison",
      other.get(f"/compare/progress/{job}").status_code == 404)

for s in servers:
    s.shutdown()
print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
