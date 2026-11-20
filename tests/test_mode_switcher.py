"""A chat's mode can be seen and changed, not just inherited.

The persona lock (test_conversation_memory.py) holds a chat to the first
specialist it reaches. Until 2026-11-19 nothing showed that mode and the only
way to change it was a new chat. This suite covers the switcher:
  - /modes lists exactly the specialists a chat can be held to (custom routes
    included; general/code/search/audio/summarize never), and any user may
    read it — /settings/routes_meta is admin-only;
  - POST /memory/<sid>/mode sets or clears it, per user, rejects non-modes,
    and clearing never creates a session; GET /memory reports it;
  - a chosen mode holds the chat like a locked one, and clearing it lets the
    next specialist question pick again;
  - when the lock overrides the classifier, /ask says which mode it wanted
    (the "switch & ask again" hint) — never reasoning, never a breakout
    action, never the mode the chat is already in;
  - the chat page carries the pill, the menu and the hint, and its script
    parses.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import router  # noqa: E402

# Throwaway stores before anything can touch the real ones (house rule).
_tmp = tempfile.mkdtemp(prefix="mode_switcher.")
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

P, F = [], []
def check(n, c, d=""):
    (P if c else F).append(n)
    print(("  PASS  " if c else "  FAIL  ") + n + (f"  [{d}]" if d and not c else ""))


TMP = tempfile.mkdtemp(prefix="mode_switcher.")
router.USERDATA_ROOT = os.path.join(TMP, "userdata")
os.makedirs(router.USERDATA_ROOT)

def _route(custom=False, color="", subject=""):
    return {"backend": "brain-x", "model": "", "subject": subject, "persona": "",
            "custom": custom, "color": color}

CFG = {
    "backends": [{"id": "brain-x", "url": "http://h-brain:8087", "key": "", "model": ""}],
    "routes": {n: _route() for n in ("recipes", "creative", "reasoning", "review",
                                     "general", "code", "search", "summarize")},
    "roles": {"classifier": "brain-x", "summarizer": "brain-x", "coder": "brain-x",
              "coder_fast": "brain-x", "coder_quick": "brain-x"},
}
CFG["routes"]["garden"] = _route(custom=True, color="#3a7d44",
                                 subject="Vegetable gardening, planting and pests.")
router._BUDGETS_READY = False          # don't probe coder windows on apply
router._apply_router_config(CFG)

real_classify = router.classify

# --- A. the route decision reports what the lock held back --------------------
print("A. route decision")
def decide(classified, mode="", q="tell me more about that"):
    held = []
    router.classify = lambda *a, **k: classified
    try:
        got = router._route_request(q, False, "", "", "", mode, held)
    finally:
        router.classify = real_classify
    return got, held

got, held = decide("creative", mode="recipes", q="write a poem about it")
check("the lock holds a recipes chat on a creative question",
      got == ("recipes", False), str(got))
check("...and reports that the classifier wanted creative", held == ["creative"], str(held))
got, held = decide("code", mode="recipes", q="write me a script")
check("a breakout action leaves the mode and holds nothing back",
      got == ("code", False) and held == [], f"{got} {held}")
got, held = decide("creative")
check("no mode: nothing is held back", got == ("creative", False) and held == [])
got, held = decide("recipes", mode="recipes")
check("agreeing with the mode holds nothing back", held == [], str(held))
router.classify = lambda *a, **k: "creative"
try:
    old = router._route_request("write a poem", False, "", "", "", "recipes")
finally:
    router.classify = real_classify
check("old callers (no held list) still work", old == ("recipes", False), str(old))

S = router._suggest_switch
check("suggest: another mode is offered", S("creative", "recipes") == "creative")
check("suggest: never INTO reasoning (every 'why…?' lands there)",
      S("reasoning", "recipes") == "")
check("suggest: OUT of reasoning is fine", S("recipes", "reasoning") == "recipes")
check("suggest: a custom mode is offered", S("garden", "recipes") == "garden")
for non in ("general", "search", "code", "audio", "summarize", "no-such-route"):
    check(f"suggest: '{non}' is not a mode, so never offered", S(non, "recipes") == "")
check("suggest: not the mode the chat is in", S("recipes", "recipes") == "")

# --- B. endpoints ---------------------------------------------------------------
print("B. /modes and /memory/<sid>/mode")
router.app.config["SESSION_COOKIE_SECURE"] = False
def login(email, admin):
    u, err = router.create_user(email, "correct-horse-battery", is_admin=admin)
    assert u, err
    c = router.app.test_client()
    c.post("/login", data={"email": email, "password": "correct-horse-battery"})
    return c, u["id"]
adm, _ = login("mode-admin@example.com", True)
usr, usr_id = login("mode-user@example.com", False)
anon = router.app.test_client()

r = usr.get("/modes")
names = [m["name"] for m in (r.get_json() or [])] if r.status_code == 200 else []
check("a non-admin can read /modes", r.status_code == 200, r.status_code)
check("/modes lists the personas, custom routes included",
      set(names) == {"recipes", "creative", "reasoning", "review", "garden"}, str(names))
garden = next((m for m in r.get_json() if m["name"] == "garden"), {})
check("a custom mode carries its color and subject",
      garden.get("color") == "#3a7d44" and garden.get("custom")
      and garden.get("subject", "").startswith("Vegetable"), json.dumps(garden))
check("/modes needs a login", anon.get("/modes").status_code == 401)

def mode_of(c, sid):
    return c.get(f"/memory/{sid}").get_json().get("mode")
def set_mode(c, sid, mode):
    return c.post(f"/memory/{sid}/mode", json={"mode": mode})

check("a fresh chat has no mode", mode_of(usr, "s1") == "")
r = set_mode(usr, "s1", "recipes")
check("setting a mode", r.status_code == 200 and r.get_json()["mode"] == "recipes",
      r.get_data(as_text=True))
check("GET /memory reports it", mode_of(usr, "s1") == "recipes")
check("a custom mode can be chosen", set_mode(usr, "s2", "garden").status_code == 200
      and mode_of(usr, "s2") == "garden")
for bad in ("general", "code", "search", "audio", "summarize", "nonsense", "<b>x</b>"):
    r = set_mode(usr, "s1", bad)
    check(f"'{bad}' is refused", r.status_code == 400 and mode_of(usr, "s1") == "recipes",
          r.status_code)
check("modes are per user: another user's chat with the same id is untouched",
      mode_of(adm, "s1") == "")
set_mode(adm, "s1", "creative")
check("...and setting theirs leaves mine alone",
      mode_of(usr, "s1") == "recipes" and mode_of(adm, "s1") == "creative")
check("setting needs a login", set_mode(anon, "s1", "recipes").status_code == 401)
r = set_mode(usr, "never-used", "")
saved = json.load(open(router.memory_file(usr_id)))
check("clearing a chat with no state succeeds without creating one",
      r.status_code == 200 and "never-used" not in saved and saved["s1"]["mode"] == "recipes",
      f"{r.status_code} {sorted(saved)}")
r = usr.post("/memory/s1/mode", json={})
check("a body without a mode clears it", r.status_code == 200 and mode_of(usr, "s1") == "")

# --- C. a chosen mode drives /ask ------------------------------------------------
print("C. /ask with a chosen mode")
real_spawn = router.spawn_owned
class _T:
    def start(self):
        pass
router.spawn_owned = lambda target=None, args=(), **kw: _T()
def ask(c, sid, q, classified):
    router.classify = lambda *a, **k: classified
    try:
        return c.post("/ask", json={"question": q, "session_id": sid}).get_json()
    finally:
        router.classify = real_classify
try:
    set_mode(usr, "c1", "recipes")
    j = ask(usr, "c1", "hi", "general")
    check("a chosen mode holds even the first message ('hi' -> recipes)",
          j["route"] == "recipes" and j["mode"] == "recipes" and j["suggest"] == "",
          json.dumps(j))
    j = ask(usr, "c1", "Write a limerick about my cat.", "creative")
    check("a held-back creative question comes with the switch hint",
          j["route"] == "recipes" and j["suggest"] == "creative", json.dumps(j))
    j = ask(usr, "c1", "Why does the sauce thicken?", "reasoning")
    check("a 'why' the classifier calls reasoning gets no hint",
          j["route"] == "recipes" and j["suggest"] == "", json.dumps(j))
    j = ask(usr, "c1", "search vegan bakeries near me", "search")
    check("a breakout to search gets no hint", j["route"] == "search"
          and j["suggest"] == "" and j["mode"] == "recipes", json.dumps(j))
    set_mode(usr, "c1", "creative")
    j = ask(usr, "c1", "Write a limerick about my cat.", "creative")
    check("after switching, the same question goes to creative with no hint",
          j["route"] == "creative" and j["mode"] == "creative" and j["suggest"] == "",
          json.dumps(j))
    set_mode(usr, "c1", "")
    j = ask(usr, "c1", "hello again", "general")
    check("cleared: a general message does not lock", j["mode"] == "" and j["route"] == "general",
          json.dumps(j))
    j = ask(usr, "c1", "A chickpea dinner?", "recipes")
    check("cleared: the next specialist question becomes the mode again",
          j["mode"] == "recipes" and mode_of(usr, "c1") == "recipes", json.dumps(j))
    j = ask(usr, "fresh", "Write a sonnet.", "creative")
    check("a chat with no mode gets no hint", j["suggest"] == "" and j["mode"] == "creative",
          json.dumps(j))
    usr.delete("/memory/c1")
    check("Clear memory also clears the mode", mode_of(usr, "c1") == "")
finally:
    router.spawn_owned = real_spawn
    router.classify = real_classify

# --- D. the page -------------------------------------------------------------------
print("D. chat page")
page = usr.get("/", headers={"Accept": "text/html"}).get_data(as_text=True)
check("header has the mode pill and its menu",
      'id="modePill"' in page and 'id="modeMenu"' in page and 'aria-haspopup="true"' in page)
check("the page reads /modes and the chat's mode, and posts changes",
      "fetch('/modes')" in page and "'/mode'" in page and "chatMode=(m&&m.mode)" in page)
check("the welcome badges and the switch hint are wired",
      "wireLegend()" in page and "function addModeSuggestion" in page
      and 'id="esSub"' in page)
check("the hint is saved with the answer and restored for the newest one only",
      "msg.suggest=suggest" in page and "function restoreSuggestion" in page)
node = shutil.which("node")
if node:
    bad = []
    for i, s in enumerate(re.findall(r"<script>(.*?)</script>", page, re.S)):
        p = os.path.join(TMP, f"s{i}.js")
        open(p, "w").write(s)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        if r.returncode:
            bad.append(r.stderr[:300])
    check("every script on the chat page parses", not bad, " | ".join(bad))
else:
    print("  SKIP  node not found — script parse check")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{len(P)} passed, {len(F)} failed")
sys.exit(1 if F else 0)
