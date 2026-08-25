"""CREWE_THINK_ROUTES: opt-in thinking for the chat routes, with a runaway guard.

Some chat templates PRE-FILL an empty thought block when thinking is
off, so a chat route answers with zero deliberation unless it opts in. Measured on a
local chat backend: thinking changed no answers on 'reasoning' and cost ~10x
on 'review', so OFF stays the default -- but the switch has to exist, and it
has to be safe when someone flips it. Verifies:
  1. route_thinks() is False for every route when the flag is unset
  2. named routes opt in; unnamed ones stay off; "all" opts everyone in
  3. parsing tolerates whitespace, case, and empty entries
  4. the chat payload carries enable_thinking matching route_thinks(route)
  5. reasoning_content never leaks into the stored answer
  6. all-thinking/no-content is aborted at THINK_MAX_TOKENS with a note naming
     the route, instead of running to the 600s transport timeout
  7. content already flowing disarms the guard no matter how much thinking follows
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="think_routes.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg
router.USERDATA_ROOT = _tmp
router.set_owner("think-routes-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

print("think routes / opt-in thinking flag")

# --- 1-3. route_thinks() semantics -------------------------------------------
_saved_routes = router.THINK_ROUTES

router.THINK_ROUTES = set()
check("unset flag: no route thinks",
      not any(router.route_thinks(r) for r in
              ("general", "reasoning", "review", "search", "summarize", "code")))

router.THINK_ROUTES = {"reasoning", "review"}
check("named routes opt in", router.route_thinks("reasoning") and router.route_thinks("review"))
check("unnamed routes stay off",
      not router.route_thinks("general") and not router.route_thinks("search"))
check("route_thinks is case-insensitive", router.route_thinks("REASONING"))
check("route_thinks tolerates None/empty", not router.route_thinks("") and not router.route_thinks(None))

router.THINK_ROUTES = {"all"}
check('"all" opts every route in',
      all(router.route_thinks(r) for r in ("general", "reasoning", "review", "search")))

_parse = lambda v: {r.strip().lower() for r in v.split(",") if r.strip()}
check("parsing strips whitespace/case/empties",
      _parse(" Reasoning , ,review,") == {"reasoning", "review"})

# --- harness for the streaming chat path -------------------------------------
def sse(delta):
    return ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()

class _Resp:
    """Enough of a requests.Response for both the streamed chat call and the
    non-streamed housekeeping calls (session summariser) run_job may make."""
    status_code = 200
    text = ""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": ""}}]}

def drive(route, lines, think_routes):
    """Run the real run_job chat path against a scripted stream; return (payload, job).

    run_job also makes housekeeping calls (the session summariser), so capture
    every POST and hand back the one aimed at THIS route's specialist."""
    calls = []
    router.THINK_ROUTES = think_routes
    def fake_post(url, json=None, headers=None, stream=None, timeout=None, **kw):
        calls.append((url, json))
        return _Resp()
    real = (router.requests.post, router._cancellable_lines,
            router.record_spend, router._append_history, router._store_files)
    router.requests.post = fake_post
    router._cancellable_lines = lambda r, job_id: iter(lines)
    router.record_spend = lambda *a, **k: 0
    router._append_history = lambda *a, **k: None
    router._store_files = lambda *a, **k: None
    job_id = f"think-{route}"
    with router.JOBS_LOCK:
        router.JOBS[job_id] = {"owner": "u", "tokens": 0, "done": False, "route": route,
                               "answer": "", "stage": "drafting", "audio": None,
                               "stage_ts": 0.0, "effort": "normal"}
    try:
        router.run_job(job_id, route, "hello", "sess-think", "normal")
    finally:
        (router.requests.post, router._cancellable_lines, router.record_spend,
         router._append_history, router._store_files) = real
    want = router.SPECIALISTS[route]
    payload = next((p for u, p in calls if u == want), None)
    with router.JOBS_LOCK:
        return payload, dict(router.JOBS[job_id])

# --- 4. payload carries the flag ---------------------------------------------
pay, _ = drive("reasoning", [sse({"content": "hi"})], {"reasoning"})
check("opted-in route sends enable_thinking true",
      pay["chat_template_kwargs"]["enable_thinking"] is True, str(pay.get("chat_template_kwargs")))

pay, _ = drive("general", [sse({"content": "hi"})], {"reasoning"})
check("opted-out route sends enable_thinking false",
      pay["chat_template_kwargs"]["enable_thinking"] is False, str(pay.get("chat_template_kwargs")))

pay, _ = drive("reasoning", [sse({"content": "hi"})], set())
check("flag unset keeps today's behaviour (thinking false)",
      pay["chat_template_kwargs"]["enable_thinking"] is False)

# --- 5. reasoning_content never reaches the answer ---------------------------
_, job = drive("reasoning",
               [sse({"reasoning_content": "secret deliberation"}),
                sse({"content": "visible "}), sse({"content": "answer"})],
               {"reasoning"})
check("thought is excluded from the stored answer",
      "secret" not in job["answer"] and "visible answer" in job["answer"], job["answer"][:80])
check("hidden reasoning still moves the token counter", job["tokens"] == 3, str(job["tokens"]))

# --- 6. runaway guard ---------------------------------------------------------
_saved_max = router.THINK_MAX_TOKENS
router.THINK_MAX_TOKENS = 5
_, job = drive("review", [sse({"reasoning_content": "hmm"})] * 40, {"review"})
check("all-thinking/no-content run is aborted", "stopped:" in job["answer"], job["answer"][:90])
check("abort note names the offending route", "review" in job["answer"], job["answer"][:90])
check("abort happens at the budget, not after the whole stream",
      job["tokens"] <= 6, f"tokens={job['tokens']}")

# --- 7. content disarms the guard --------------------------------------------
_, job = drive("review",
               [sse({"content": "A"})] + [sse({"reasoning_content": "hmm"})] * 40,
               {"review"})
check("content already flowing disarms the guard",
      "stopped:" not in job["answer"] and job["answer"].strip() == "A", job["answer"][:90])

router.THINK_MAX_TOKENS = _saved_max
router.THINK_ROUTES = _saved_routes
print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
