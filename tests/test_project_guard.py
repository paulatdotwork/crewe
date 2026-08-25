"""An ACTIVE BUILD must not be answered by a route that cannot see it.

Regression: a live 11-file web project was in
sess["files"], the user asked "please add settings so I can change wall paper",
the classifier called it conversation, and the chat route -- which never
injects file CONTENTS, only the code pipeline does -- answered from the file
names in a 2000-char-truncated history by inventing a single-file replacement
app, then rationalised it ("too large for one code block"). Verifies:
  1. an edit request against a session WITH files is forced onto the code route
  2. the same request with NO stored files is left to the classifier
  3. a question ABOUT the build ("why did you remove everything?") is NOT
     treated as a build request, even though it contains edit verbs
  4. error pastes still reach code (the pre-existing _ERRORISH path)
  5. a chat route serving a session with files is told the project exists,
     with names and sizes and an explicit do-not-rewrite instruction
  6. a chat route with no stored files gets no manifest and no extra tokens
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="project_guard.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg
router.USERDATA_ROOT = _tmp
router.set_owner("project-guard-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

print("project guard / active-build routing and manifest")

# --- 1-4. the routing decision, exactly as /ask makes it ---------------------
def decide(q, has_files):
    """Mirror of the /ask precedence chain for the two state-driven rules."""
    if has_files and router._ERRORISH.search(q):
        return "code"
    if (has_files and router._EDITISH.search(q)
            and not router._ASKS_ABOUT.match(q)):
        return "code"
    return "classifier"

THE_ASK = ("please add settings so I can change wall paper and also set "
           "temperature units. Also, please add a special feature")
THE_CHALLENGE = "why did you remove everything else when you added settings"

check("the request that lost the webOS now routes to code",
      decide(THE_ASK, True) == "code")
check("same request with no active build is left to the classifier",
      decide(THE_ASK, False) == "classifier")
check("'why did you remove everything' is NOT a build request",
      decide(THE_CHALLENGE, True) == "classifier")
check("error paste still reaches code",
      decide("Uncaught TypeError: Cannot set properties of undefined", True) == "code")

EDITS = ["can you make the taskbar taller", "get rid of the pomodoro app",
         "bring back the file explorer", "wire up the settings to localStorage",
         "rename the weather app", "please fix the clock alignment"]
check("plain-language edit requests route to code",
      all(decide(q, True) == "code" for q in EDITS),
      str([q for q in EDITS if decide(q, True) != "code"]))

ASKS = ["what happened to my terminal app?", "did you delete the weather module?",
        "explain how the focus mode works", "what is the capital of France",
        "how does a bloom filter work", "I thought you were going to add dark mode"]
check("questions and chit-chat are not hijacked",
      all(decide(q, True) == "classifier" for q in ASKS),
      str([q for q in ASKS if decide(q, True) != "classifier"]))

# --- 5-6. the manifest a chat route now carries ------------------------------
class _Resp:
    status_code = 200
    text = ""
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def raise_for_status(self): pass
    def json(self): return {"choices": [{"message": {"content": ""}}]}

def system_prompt_for(route, files):
    """Run the real chat path and hand back the system prompt it actually sent."""
    calls = []
    def fake_post(url, json=None, headers=None, stream=None, timeout=None, **kw):
        calls.append((url, json)); return _Resp()
    real = (router.requests.post, router._cancellable_lines, router.record_spend,
            router._append_history, router._store_files)
    router.requests.post = fake_post
    router._cancellable_lines = lambda r, job_id: iter(
        [b'data: ' + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode()])
    router.record_spend = lambda *a, **k: 0
    router._append_history = lambda *a, **k: None
    router._store_files = lambda *a, **k: None
    sid = f"guard-{route}-{len(files)}"
    with router.SESSIONS_LOCK:
        router.sessions()[sid] = {"history": [], "summary": "", "files": dict(files)}
    job_id = f"guard-job-{route}-{len(files)}"
    with router.JOBS_LOCK:
        router.JOBS[job_id] = {"owner": "u", "tokens": 0, "done": False, "route": route,
                               "answer": "", "stage": "drafting", "audio": None,
                               "stage_ts": 0.0, "effort": "normal"}
    try:
        router.run_job(job_id, route, "how does this work?", sid, "normal")
    finally:
        (router.requests.post, router._cancellable_lines, router.record_spend,
         router._append_history, router._store_files) = real
    want = router.SPECIALISTS[route]
    payload = next((p for u, p in calls if u == want), None)
    return payload["messages"][0]["content"]

PROJECT = {"index.html": "x" * 14175, "js/main.js": "y" * 8588, "js/fs.js": "z" * 4772}
sysp = system_prompt_for("general", PROJECT)
check("chat route is told an active project exists", "ACTIVE PROJECT" in sysp)
check("manifest names every file",
      all(n in sysp for n in PROJECT), sysp[-400:])
check("manifest carries sizes, so truncation is visible", "14,175" in sysp)
check("manifest forbids the rewrite that caused this",
      "Never rewrite" in sysp and "single-file" in sysp)
check("manifest says the contents are NOT visible",
      "cannot see" in sysp.lower())

bare = system_prompt_for("general", {})
check("no stored files means no manifest", "ACTIVE PROJECT" not in bare)
check("bare session keeps the plain persona",
      bare.strip() == router.SYSTEM_PROMPTS["general"].strip(), bare[:120])

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
