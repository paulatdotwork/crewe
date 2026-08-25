"""History must not silently hand a fragment back as if it were whole.

HISTORY_CHARS was 2000, which cut a code answer off mid-tag
("...<script src=\"js/game.js\"></script>\\n  <sc") and gave the reader NO sign
anything was missing. A model replayed that stub, believed it was the whole
project, and rewrote it from scratch. Verifies:
  1. a long answer is stored far more completely than the old 2000 cap
  2. truncation cuts on a LINE boundary, never mid-token
  3. truncation SAYS SO, with the number of characters dropped
  4. a short answer is stored byte-exact, with no marker added
  5. replay is budgeted, so a bigger store cannot overflow a small backend
  6. replay keeps the NEWEST turns and drops the oldest
  7. a short conversation still replays in full
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="suite.")
router.USERS_FILE = os.path.join(_tmp, "users.json")
_cfg = os.path.join(_tmp, "router_config.json")
try:
    import shutil
    shutil.copyfile(router.ROUTER_CONFIG_FILE, _cfg)
except OSError:
    import json as _json
    _json.dump(router.ROUTER_CONFIG, open(_cfg, "w"))
router.ROUTER_CONFIG_FILE = _cfg

router.USERDATA_ROOT = tempfile.mkdtemp(prefix="history_budget.")
router.set_owner("history-budget-test-user")

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))

print("history budget / visible truncation")

# the exact shape that caused the loss: a page listing script tags
HTML = ("<html>\n"
        + "".join(f'  <script src="js/mod{i}.js"></script>\n' for i in range(400))
        + "</html>\n")

clip = router._clip_for_history(HTML)
check("a long answer is kept far beyond the old 2000-char cap",
      len(clip) > 8000, f"{len(clip)} chars")
check("truncation is announced", "truncated" in clip, clip[-120:])
check("the dropped amount is stated",
      "more characters" in clip, clip[-120:])
body = clip.split("[... truncated")[0]
check("the cut lands on a line boundary, never mid-tag",
      body.rstrip().endswith(">"), repr(body[-60:]))

short = "def f():\n    return 1\n"
check("a short answer is stored byte-exact",
      router._clip_for_history(short) == short, repr(router._clip_for_history(short)))
check("...with no truncation marker invented",
      "truncated" not in router._clip_for_history(short))

# --- replay budget ----------------------------------------------------------
big = [{"q": f"question {i}", "a": "A" * 5000} for i in range(6)]
msgs = router._history_messages(big)
total = sum(len(m["content"]) for m in msgs)
check("replay stays inside the budget",
      total <= router.HISTORY_REPLAY_CHARS, f"{total} > {router.HISTORY_REPLAY_CHARS}")
check("replay keeps the NEWEST turn",
      msgs and msgs[-2]["content"] == "question 5", str(msgs[-2] if msgs else None))
check("replay drops the oldest, not slices every turn",
      all(len(m["content"]) in (5000, len("question 5")) or m["content"].startswith("question")
          for m in msgs), str([len(m["content"]) for m in msgs]))
check("a short conversation replays in full",
      len(router._history_messages([{"q": "hi", "a": "there"}] * 6)) == 12)
check("empty history is handled", router._history_messages([]) == []
      and router._history_messages(None) == [])

# --- the stored round trip --------------------------------------------------
SID = "history-budget-session"
with router.SESSIONS_LOCK:
    router.sessions()[SID] = {"history": [], "files": {}}
router._append_history(SID, "build me a page", HTML, "code")
with router.SESSIONS_LOCK:
    stored = router.sessions()[SID]["history"][-1]["a"]
check("the stored turn carries the truncation marker",
      "truncated" in stored, stored[-100:])
check("the stored turn is much larger than the old cap",
      len(stored) > 8000, f"{len(stored)} chars")

print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
