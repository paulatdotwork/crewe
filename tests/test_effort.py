"""Effort levels: two coders, per-level budgets, and the request plumbing.

Mirrors the house style of test_auth.py / test_tenancy.py: a throwaway user
store, a Flask test client, plain PASS/FAIL lines.
"""
import os, sys, json, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router

tmpdir = tempfile.mkdtemp()
router.USERS_FILE = os.path.join(tmpdir, "users.json")
router.app.config["SESSION_COOKIE_SECURE"] = False
# Never let a test write the real router_config.json — one already did.
import shutil as _sh
_cfgcopy = os.path.join(tmpdir, "router_config.json")
if os.path.exists(router.ROUTER_CONFIG_FILE):
    _sh.copyfile(router.ROUTER_CONFIG_FILE, _cfgcopy)
else:                       # fresh clone: no config yet -> built-in defaults
    json.dump(router.ROUTER_CONFIG, open(_cfgcopy, "w"))
router.ROUTER_CONFIG_FILE = _cfgcopy

P, F = [], []
def check(name, cond, detail=""):
    (P if cond else F).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (f"   [{detail}]" if detail and not cond else ""))

c = router.app.test_client()

# ---------------------------------------------------------------- pure layer
print("--- effort resolution ---")
for lvl in ("fast", "normal", "extra"):
    router.set_effort(lvl)
    check(f"set_effort('{lvl}') sticks", router._effort() == lvl, router._effort())
router.set_effort("normal")
check("legacy 'easy' aliases to normal (saved browsers keep working)",
      router._effort() == "normal", router._effort())
router.set_effort("extra")
check("legacy 'hard' aliases to extra", router._effort() == "extra", router._effort())
router.set_effort("BOGUS")
check("unknown level normalises to the default (normal — never the paid tier)",
      router._effort() == "normal", router._effort())
router.set_effort("")
check("empty level normalises to normal", router._effort() == "normal", router._effort())

seen = {}
t = threading.Thread(target=lambda: seen.setdefault("lvl", router._effort()))
t.start(); t.join()
check("a fresh thread defaults to normal (no raise, unlike _owner)",
      seen.get("lvl") == "normal", str(seen))

print("--- the two coders are actually different ---")
router.set_effort("fast");   fast_url = router.coder_url()
router.set_effort("normal"); easy_url = router.coder_url()
router.set_effort("extra");  hard_url = router.coder_url()
check("fast resolves to CODE_AGENT_URL_QUICK",
      fast_url == router.CODE_AGENT_URL_QUICK, fast_url)
check("normal resolves to CODE_AGENT_URL_FAST",
      easy_url == router.CODE_AGENT_URL_FAST, easy_url)
check("extra resolves to CODE_AGENT_URL",
      hard_url == router.CODE_AGENT_URL, hard_url)
check("the live config gives them distinct backends",
      easy_url != hard_url, f"both {easy_url}")

print("--- budgets follow the model, not the level name ---")
router.set_effort("normal")
e_proj, e_hard, e_steps = (router.small_project_chars(),
                           router.step_file_hard(), router.max_plan_steps())
router.set_effort("extra")
h_proj, h_hard, h_steps = (router.small_project_chars(),
                           router.step_file_hard(), router.max_plan_steps())
# NOT "easy < hard": budgets follow each backend's real context window, and
# the operator may legitimately put same-sized models on two tiers (that
# exact wiring is live right now). Assert consistency with the probe instead.
for lvl, url_ in (("normal", router.CODE_AGENT_URL_FAST),
                  ("extra", router.CODE_AGENT_URL)):
    router.set_effort(lvl)
    want = router._budgets_for(url_)[0]["small_project_chars"]
    check(f"{lvl} budget matches its own backend's probe",
          router.small_project_chars() == want,
          f"{router.small_project_chars()} vs {want}")
check("step_file_hard is 2x the project budget (easy)", e_hard == e_proj * 2,
      f"{e_hard} vs {e_proj}")
check("step_file_hard is 2x the project budget (hard)", h_hard == h_proj * 2,
      f"{h_hard} vs {h_proj}")
check("easy plans fewer steps", e_steps < h_steps, f"{e_steps} vs {h_steps}")
check("step_max_tokens stays UNCAPPED on both levels (a cap destroys files)",
      router.EFFORT_TUNING["normal"]["step_max_tokens"] == 0 and
      router.EFFORT_TUNING["extra"]["step_max_tokens"] == 0)

# budgets must be sized from each backend's REAL context window
easy_ctx = router._backend_ctx(router.CODE_AGENT_URL_FAST)
if easy_ctx:
    check("easy budget fits inside the easy coder's context",
          e_proj <= easy_ctx * 4, f"{e_proj} chars vs ctx {easy_ctx} tokens")

print("--- coder_fast absent => both levels use the deep coder ---")
saved_fast = router.CODE_AGENT_URL_FAST
cfg = json.loads(json.dumps(router.ROUTER_CONFIG))
# Restore the operator's ACTUAL role, not a hardcoded backend id — this test
# starts from the live config, which they are free to rewire.
_orig_fast_role = cfg["roles"].get("coder_fast")
cfg["roles"].pop("coder_fast", None)
router._apply_router_config(cfg)
router.set_effort("normal"); fallback_easy = router.coder_url()
check("with no coder_fast role, easy falls back to the deep coder",
      fallback_easy == router.CODE_AGENT_URL, fallback_easy)
check("...and its budgets match the deep coder's",
      router.small_project_chars() == h_proj,
      f"{router.small_project_chars()} vs {h_proj}")
# restore
if _orig_fast_role:
    cfg["roles"]["coder_fast"] = _orig_fast_role
router._apply_router_config(cfg)
router.set_effort("normal")
check("restoring the role restores the split coder",
      router.coder_url() == saved_fast, router.coder_url())

# ---------------------------------------------------------------- HTTP layer
print("--- /ask plumbing ---")
admin, err = router.create_user("admin@example.com", "correct-horse-battery", is_admin=True)
assert admin, err
c.post("/login", data={"email": "admin@example.com", "password": "correct-horse-battery"})

captured = {}
real_spawn = router.spawn_owned
def fake_spawn(target=None, args=(), **kw):
    captured["target"], captured["args"] = target, args
    class _T:
        def start(self): pass
    return _T()
router.spawn_owned = fake_spawn
real_classify = router.classify
router.classify = lambda *a, **k: "general"
try:
    r = c.post("/ask", json={"question": "hello", "session_id": "s1", "effort": "easy"})
    check("/ask echoes the effort back", r.get_json().get("effort") == "normal",
          json.dumps(r.get_json()))
    check("effort is passed to run_job", captured["args"][-1] in ("easy", "normal"),
          str(captured["args"]))
    jid = r.get_json()["job_id"]
    check("effort is recorded on the job", router.JOBS[jid].get("effort") in ("easy", "normal"),
          str(router.JOBS[jid].get("effort")))

    r = c.post("/ask", json={"question": "hello", "session_id": "s2"})
    check("absent effort defaults to normal (never the paid tier)",
          r.get_json().get("effort") == "normal", json.dumps(r.get_json()))

    r = c.post("/ask", json={"question": "hello", "session_id": "s3", "effort": "wildly-invalid"})
    check("invalid effort is normalised, not rejected",
          r.get_json().get("effort") == "normal", json.dumps(r.get_json()))
finally:
    router.spawn_owned = real_spawn
    router.classify = real_classify

print("--- run_job captures effort onto its own thread ---")
router.set_effort("extra")           # caller thread stays hard
got = {}
def fake_pipeline(job_id, question, session_id):
    got["lvl"] = router._effort()
    got["url"] = router.coder_url()
real_pipeline = router.run_code_pipeline
router.run_code_pipeline = fake_pipeline
try:
    th = threading.Thread(target=router.run_job,
                          args=("j-test", "code", "build me a thing", "s9", "easy"))
    th.start(); th.join()
    check("run_job sets effort on the worker thread", got.get("lvl") == "normal", str(got))
    check("...so the pipeline sees the easy coder",
          got.get("url") == router.CODE_AGENT_URL_FAST, str(got))
    check("the calling thread is unaffected", router._effort() == "extra", router._effort())
finally:
    router.run_code_pipeline = real_pipeline

print("--- settings validation ---")
good = json.loads(json.dumps(router.ROUTER_CONFIG))
r = c.post("/settings/config", json=good)
check("saving a config containing coder_fast is accepted",
      r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:120]}")
bad = json.loads(json.dumps(router.ROUTER_CONFIG))
bad["roles"]["coder_fast"] = "no-such-backend"
r = c.post("/settings/config", json=bad)
check("unknown coder_fast backend is rejected",
      r.status_code == 400 and "coder_fast" in r.get_data(as_text=True),
      f"{r.status_code} {r.get_data(as_text=True)[:160]}")
nofast = json.loads(json.dumps(router.ROUTER_CONFIG))
nofast["roles"].pop("coder_fast", None)
r = c.post("/settings/config", json=nofast)
check("a config with NO coder_fast is still valid (back-compat)",
      r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:120]}")

# put the real config back on disk exactly as we found it
router._save_router_config(good)
router._apply_router_config(good)

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
