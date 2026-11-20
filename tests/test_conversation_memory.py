"""A conversation must remember itself and keep the persona it started with.

Three bugs, found 2026-11-19:
  A. The rolling summary was EMPTY in 102 of 112 summarized sessions. The
     Gemma 4 E4B summarizer thinks by default; its 200-token budget went to
     "Thinking Process:" and the content came back '' — which then overwrote
     any earlier good summary.
  B. History was 6 turns / a fixed 24k chars whatever the model. That threw
     away ~95% of a 128k window, yet could overflow an 8k E4B.
  C. Every message was re-classified from scratch, so a recipes chat drifted
     to general/reasoning mid-conversation and lost its persona.
Verifies each fix, plus that the older routing rules still win where they
must (active builds, short continuations).
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="conversation_memory.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    import json as _json
    _json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg
router.USERDATA_ROOT = _tmp

router.USERDATA_ROOT = tempfile.mkdtemp(prefix="conversation_memory.")
router.set_owner("conversation-memory-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))


class _Resp:
    def __init__(self, content):
        self._c = content
        self.text = json.dumps(self.json())
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
    def raise_for_status(self):
        pass
    def json(self):
        return {"choices": [{"message": {"content": self._c},
                             "finish_reason": "stop"}]}


def run_summarize(reply, question="q", answer="a", start=None):
    """Run summarize() against a fake summarizer; return (payload, session)."""
    sid = f"sum-{len(P) + len(F)}"
    with router.SESSIONS_LOCK:
        router.sessions()[sid] = {"summary": start or "", "turns": 0}
    seen = {}
    real = router.requests.post
    def fake_post(url, json=None, **kw):
        seen["payload"] = json
        return _Resp(reply)
    router.requests.post = fake_post
    try:
        router.summarize(sid, question, answer)
    finally:
        router.requests.post = real
    with router.SESSIONS_LOCK:
        return seen.get("payload") or {}, dict(router.sessions()[sid])


# --- A. summarizer -----------------------------------------------------------
print("A. summarizer")
payload, sess = run_summarize("User wants a chickpea curry.")
check("thinking is switched OFF for the summarizer",
      (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is False,
      json.dumps(payload.get("chat_template_kwargs")))
check("a real reply is stored", sess["summary"] == "User wants a chickpea curry.",
      repr(sess["summary"]))
check("turns counts the update", sess["turns"] == 1, str(sess["turns"]))

payload, sess = run_summarize("", start="Earlier: user is vegetarian.")
check("an EMPTY reply keeps the previous summary (no more '' wipes)",
      sess["summary"] == "Earlier: user is vegetarian.", repr(sess["summary"]))
check("...and does not count as a turn", sess["turns"] == 0, str(sess["turns"]))

payload, sess = run_summarize("<think>hmm</think>Kept: likes spice.")
check("leaked think-text is stripped from the stored summary",
      sess["summary"] == "Kept: likes spice.", repr(sess["summary"]))

long_answer = "intro\n" + "".join(f"step {i}: add {i} g of salt\n" for i in range(300))
payload, _ = run_summarize("ok", answer=long_answer)
user_msg = payload["messages"][1]["content"]
check("the summarizer reads far past the old 800-char cut",
      "step 100:" in user_msg, f"{len(user_msg)} chars sent")
check("...but stays inside an 8k model (bounded, and says it was cut)",
      len(user_msg) < 9000 and "truncated" in user_msg, f"{len(user_msg)} chars")

# --- B. history sized to the model -------------------------------------------
print("B. history budget")
check("20 exchanges are kept per session (was 6)", router.HISTORY_TURNS >= 20,
      str(router.HISTORY_TURNS))

real_ctx = router._backend_ctx
probes = []
def fake_ctx(url, timeout=6):
    probes.append(url)
    return {"http://small/v1/chat/completions": 8192,
            "http://big/v1/chat/completions": 131072,
            "http://huge/v1/chat/completions": 1_000_000}.get(url)
router._backend_ctx = fake_ctx
router._CTX_CACHE.clear()
env_saved = os.environ.pop("CREWE_HISTORY_REPLAY_CHARS", None)
try:
    small = router._history_budget("http://small/v1/chat/completions")
    big = router._history_budget("http://big/v1/chat/completions")
    huge = router._history_budget("http://huge/v1/chat/completions")
    dead = router._history_budget("http://dead/v1/chat/completions")
    check("an 8k model gets LESS than the old fixed 24k (no overflow)",
          small < 24000 and small >= 4000, str(small))
    check("a 128k model gets far more than the old 24k",
          big > 150000, str(big))
    check("a 1M model is capped", huge == router.HISTORY_REPLAY_MAX, str(huge))
    check("an unknown window falls back to the old default",
          dead == router.HISTORY_REPLAY_CHARS, str(dead))
    check("attached documents shrink the history share",
          router._history_budget("http://big/v1/chat/completions", True) < big)

    n = len(probes)
    for _ in range(5):
        router._history_budget("http://dead/v1/chat/completions")
        router._history_budget("http://big/v1/chat/completions")
    check("the context probe is cached (a dead backend is not re-probed per message)",
          len(probes) == n, f"{len(probes) - n} extra probes")

    os.environ["CREWE_HISTORY_REPLAY_CHARS"] = "12345"
    check("an explicit CREWE_HISTORY_REPLAY_CHARS still wins",
          router._history_budget("http://big/v1/chat/completions")
          == router.HISTORY_REPLAY_CHARS)
finally:
    os.environ.pop("CREWE_HISTORY_REPLAY_CHARS", None)
    if env_saved is not None:
        os.environ["CREWE_HISTORY_REPLAY_CHARS"] = env_saved
    router._backend_ctx = real_ctx
    router._CTX_CACHE.clear()

turns = [{"q": f"q{i}", "a": "x" * 3000} for i in range(20)]
check("a big budget replays a long conversation in full",
      len(router._history_messages(turns, 200000)) == 40)
fat = [{"q": "Q" * 12000, "a": "A" * 12000}]
msgs = router._history_messages(fat, 13104)
total = sum(len(m["content"]) for m in msgs)
check("a single oversized newest turn is clipped to fit a small model",
      total <= 13104 + 400, f"{total} chars")
check("...visibly", "truncated" in msgs[-1]["content"])

SID = "keep-twenty"
with router.SESSIONS_LOCK:
    router.sessions()[SID] = {"history": []}
for i in range(25):
    router._append_history(SID, f"q{i}", f"a{i}", "recipes")
with router.SESSIONS_LOCK:
    hist = router.sessions()[SID]["history"]
check("storage keeps the newest HISTORY_TURNS exchanges",
      len(hist) == router.HISTORY_TURNS and hist[-1]["q"] == "q24",
      f"{len(hist)} kept, last {hist[-1]['q']}")

# --- B2. run_job: summary only when the replay does not cover everything ----
print("B2. run_job prompt")

class _Stream:
    status_code = 200
    headers = {"content-type": "text/event-stream"}
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def raise_for_status(self):
        pass
    def iter_lines(self):
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield b"data: [DONE]"
    def close(self):
        pass


def prompt_for(history, summary, turns, budget):
    sid = f"rj-{len(P) + len(F)}"
    with router.SESSIONS_LOCK:
        router.sessions()[sid] = {"history": list(history), "summary": summary,
                                  "turns": turns}
    jid = f"job-{sid}"
    with router.JOBS_LOCK:
        router.JOBS[jid] = {"owner": router._owner(), "tokens": 0, "done": False,
                            "route": "recipes", "answer": "", "stage": "drafting",
                            "audio": None, "stage_ts": 0}
    seen = {}
    real_post, real_spawn, real_budget = (router.requests.post, router.spawn_owned,
                                          router._history_budget)
    def fake_post(url, json=None, **kw):
        seen.setdefault("payload", json)
        return _Stream()
    router.requests.post = fake_post
    router.spawn_owned = lambda target=None, args=(), **kw: _T()
    router._history_budget = lambda url, with_uploads=False: budget
    try:
        router.run_job(jid, "recipes", "and for dessert?", sid)
    finally:
        router.requests.post, router.spawn_owned, router._history_budget = (
            real_post, real_spawn, real_budget)
    return seen["payload"]["messages"]


class _T:
    def start(self):
        pass

SUMMARY = "User is vegetarian and cooking for six."
hist = [{"q": f"q{i}", "a": "a" * 100} for i in range(5)]
msgs = prompt_for(hist, SUMMARY, 5, 100000)
check("every exchange replayed: the redundant summary is left out "
      "(keeps llama.cpp's prompt cache valid)", SUMMARY not in msgs[0]["content"])
check("...and all of history is there", len(msgs) == 1 + 10 + 1, str(len(msgs)))
msgs = prompt_for(hist, SUMMARY, 5, 300)
check("history cut for budget: the summary is included",
      SUMMARY in msgs[0]["content"])
msgs = prompt_for(hist, SUMMARY, 30, 100000)
check("older exchanges already rotated out of storage: the summary is included",
      SUMMARY in msgs[0]["content"])

# --- C. the conversation keeps its persona -----------------------------------
print("C. persona lock")
real_classify = router.classify
def route_with(classified, mode="", has_files=False, last_route="",
               q="tell me more about that"):
    router.classify = lambda *a, **k: classified
    try:
        return router._route_request(q, has_files, last_route, "", "", mode)
    finally:
        router.classify = real_classify

for drift in ("general", "reasoning", "creative"):
    if drift == "recipes" or drift not in router.SPECIALISTS:
        continue
    check(f"a recipes chat stays recipes when the classifier says {drift}",
          route_with(drift, mode="recipes") == ("recipes", False),
          str(route_with(drift, mode="recipes")))
for action in ("code", "summarize", "audio"):
    check(f"breakout action '{action}' still leaves the mode for one message",
          route_with(action, mode="recipes")[0] == action,
          str(route_with(action, mode="recipes")))
# live 2026-11-19: the classifier sent this recipes follow-up to search
check("an ordinary follow-up the classifier calls 'search' stays in the mode",
      route_with("search", mode="recipes",
                 q="How long will the leftovers keep in the fridge?")
      == ("recipes", False))
for q in ("What's the current price of a can of chickpeas at Walmart?",
          "search for vegan bakeries near me",
          "any news on the chickpea shortage?",
          "look up https://example.com/recipe"):
    check(f"a real web request breaks out to search: {q[:40]}",
          route_with("search", mode="recipes", q=q) == ("search", False))
check("with NO mode, the classifier's search is not second-guessed",
      route_with("search", q="How long will the leftovers keep?")
      == ("search", False))
check("no mode yet: the classifier decides",
      route_with("creative") == ("creative", False))
check("'general' is never a mode (it is the classifier's fallback)",
      not router._is_mode("general"))
check("a mode that no longer exists (deleted custom route) does not lock",
      route_with("creative", mode="no-such-route") == ("creative", False))
check("an active build still wins over the mode",
      route_with("recipes", mode="recipes", has_files=True,
                 q="add a dark mode toggle") == ("code", False))
check("a short continuation still follows the previous route",
      route_with("recipes", mode="recipes", last_route="search", q="more")
      == ("search", False))

# --- /ask records the mode on the session ------------------------------------
print("C. /ask sets the mode")
router.app.config["SESSION_COOKIE_SECURE"] = False
c = router.app.test_client()
admin, err = router.create_user("mode-admin@example.com", "correct-horse-battery",
                                is_admin=True)
assert admin, err
c.post("/login", data={"email": "mode-admin@example.com",
                       "password": "correct-horse-battery"})
real_spawn = router.spawn_owned
class _T:
    def start(self):
        pass
router.spawn_owned = lambda target=None, args=(), **kw: _T()
seq = iter(["general", "recipes", "reasoning", "search", "creative"])
router.classify = lambda *a, **k: next(seq)
try:
    got = [c.post("/ask", json={"question": q, "session_id": "mode-s1"}).get_json()
           for q in ("hi", "a chickpea dinner?", "why does it thicken",
                     "search chickpea prices near me", "write a haiku about it")]
finally:
    router.spawn_owned = real_spawn
    router.classify = real_classify
routes = [g["route"] for g in got]
check("'hi' (general) does not lock the chat", got[0]["mode"] == "", json.dumps(got[0]))
check("the first specialist becomes the mode", got[1]["mode"] == "recipes",
      json.dumps(got[1]))
check("drift is held: reasoning -> recipes, creative -> recipes",
      routes == ["general", "recipes", "recipes", "search", "recipes"], str(routes))
check("the mode survives a search breakout", got[4]["mode"] == "recipes",
      json.dumps(got[4]))

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
